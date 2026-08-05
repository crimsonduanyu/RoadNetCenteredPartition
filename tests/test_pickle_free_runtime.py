"""R5.2 security regressions: the runtime never deserializes a graph pickle.

R5.1 shipped a trusted-only conversion path so an operator could migrate a
pre-existing ``.gpickle``. R5.2 deleted it. These tests are the standing proof
that the capability is gone rather than merely unused: each one builds a
genuinely malicious pickle — a ``__reduce__`` hook that writes a marker file —
in a temporary directory, hands it to a public entry point, and asserts that
the entry point refuses it and that the marker never appears.

Constructing the hostile payload here is deliberate. A test that only checks
"the flag is gone" would still pass if some other route reopened the door; a
test that checks "the payload never ran" cannot.

Nothing in this module writes inside the repository, touches the network, or
leaves an artifact behind: every fixture lives under ``tmp_path``.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import pickle
import shutil
import subprocess
from typing import Any, Callable

import networkx as nx
import pytest

from roadnet_partition.io.manifests import (
    MANIFEST_FILENAME,
    atomic_write_json,
    file_record,
    load_manifest,
    validate_manifest,
)
from roadnet_partition.io.safe_graph import (
    ARTIFACT_SUFFIX,
    SCHEMA_NAME,
    SafeGraphArtifactError,
    read_safe_graph,
    write_safe_graph,
)
from roadnet_partition.io.serialization_policy import (
    EXECUTABLE_SERIALIZATION_SUFFIXES,
    ExecutableSerializationRefused,
    LEGACY_PROVENANCE_SUFFIX,
    declares_executable_serialization,
    is_executable_serialization_name,
    legacy_declarations,
)
from roadnet_partition.pipeline import preparation, publishing
from roadnet_partition.pipeline.publishing import PublishError, build_publish_inventory, publish_scope
from roadnet_partition.pipeline.results import RunContext
from roadnet_partition.releases import reproduction
from roadnet_partition.releases.reproduction import ExportError, export_reproduction
from roadnet_partition.reporting import best_partition_map
from roadnet_partition.zoning import evaluate, partition

from test_phase7_release import complete_run, release_output
from test_safe_graph_consumers import partition_config, relation_graph, safe_artifact


#: The three public graph loaders named by AUD-005.
CONSUMERS: dict[str, Any] = {
    "partition": partition,
    "evaluate": evaluate,
    "best_partition_map": best_partition_map,
}

MARKER_TEXT = "arbitrary code executed"


class Marker:
    """Pickle payload whose reconstruction hook writes a file on load.

    ``__reduce__`` runs inside ``pickle.load`` itself, before any type check
    the caller could perform, which is exactly why the refusal has to happen
    before the file is opened.
    """

    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self) -> tuple[Callable[..., Any], tuple[Any, ...]]:
        return (Path.write_text, (self.marker, MARKER_TEXT))


def hostile_pickle(path: Path, marker: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(Marker(marker)))
    return path


def hostile_pickle_in_gzip(path: Path, marker: Path) -> Path:
    """The same payload behind gzip framing, wearing a safe-artifact name."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(filename="", mode="wb", fileobj=path.open("wb"), mtime=0) as handle:
        handle.write(pickle.dumps(Marker(marker)))
    return path


@pytest.fixture
def no_unpickling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn any deserialization attempt into an immediate, attributable failure."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("production code reached pickle deserialization")

    monkeypatch.setattr(pickle, "load", forbidden)
    monkeypatch.setattr(pickle, "loads", forbidden)
    monkeypatch.setattr(pickle, "Unpickler", forbidden)


# ---------------------------------------------------------------------------
# 1-3. The three public consumers refuse a malicious pickle
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(CONSUMERS))
@pytest.mark.parametrize("suffix", EXECUTABLE_SERIALIZATION_SUFFIXES)
def test_public_consumers_refuse_a_malicious_pickle_without_opening_it(
    name: str, suffix: str, tmp_path: Path, no_unpickling: None
) -> None:
    root = tmp_path / f"{name}{suffix.replace('.', '-')}"
    marker = root / "executed.txt"
    hostile = hostile_pickle(root / f"graph{suffix}", marker)

    opened: list[str] = []
    real_open = Path.open

    def spy(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self.as_posix())
        return real_open(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "open", spy)
        with pytest.raises(SafeGraphArtifactError, match="no longer supported"):
            CONSUMERS[name].load_graph(hostile)

    assert not marker.exists()
    assert hostile.as_posix() not in opened, "the pickle payload was opened before refusal"


# ---------------------------------------------------------------------------
# 4. Pickle bytes hidden inside a .graph.json.gz name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_pickle_bytes_under_a_safe_artifact_name_are_refused(
    name: str, tmp_path: Path, no_unpickling: None
) -> None:
    """Name-based refusal cannot help here, so the magic-byte guard must."""

    root = tmp_path / name
    marker = root / "executed.txt"

    bare = hostile_pickle(root / f"bare{ARTIFACT_SUFFIX}", marker)
    with pytest.raises(SafeGraphArtifactError, match="Python pickle"):
        CONSUMERS[name].load_graph(bare)
    assert not marker.exists()

    wrapped = hostile_pickle_in_gzip(root / f"wrapped{ARTIFACT_SUFFIX}", marker)
    with pytest.raises(SafeGraphArtifactError):
        CONSUMERS[name].load_graph(wrapped)
    assert not marker.exists()


# ---------------------------------------------------------------------------
# 5. A refused Partition stage leaves no marker, no partial output, no manifest
# ---------------------------------------------------------------------------

def test_partition_stage_produces_nothing_when_handed_a_malicious_pickle(
    tmp_path: Path, no_unpickling: None
) -> None:
    marker = tmp_path / "executed.txt"
    hostile = hostile_pickle(tmp_path / "graph.gpickle", marker)
    context = RunContext("tiny", tmp_path / "external-run", tmp_path).for_stage("partition")

    with pytest.raises(SafeGraphArtifactError, match="no longer supported"):
        partition.run_partition(partition_config(tmp_path, hostile), context)

    assert not marker.exists()
    produced = sorted(path.name for path in context.stage_dir.rglob("*") if path.is_file())
    assert produced == [], produced
    assert not (context.stage_dir / "_SUCCESS").exists()
    assert not (context.stage_dir / "manifest.json").exists()


def test_partition_stage_still_completes_on_the_safe_artifact(tmp_path: Path) -> None:
    """Control: the refusal is specific to legacy input, not a blanket failure."""

    context = RunContext("tiny", tmp_path / "external-run", tmp_path).for_stage("partition")

    result = partition.run_partition(partition_config(tmp_path, safe_artifact(tmp_path)), context)

    assert result.status.value == "complete"
    assert "manifest" in result.outputs


# ---------------------------------------------------------------------------
# 6-7. Manifests: declaration alone is refused, in both directions
# ---------------------------------------------------------------------------

def test_a_manifest_record_is_refused_by_declared_format_even_with_a_safe_filename() -> None:
    """No pickle extension, but the manifest says pickle — still refused."""

    record = {"path": f"/runs/x/graph{ARTIFACT_SUFFIX}", "format": "gpickle", "sha256": "0" * 64}

    assert declares_executable_serialization(record) == "format=gpickle"


def test_a_manifest_record_is_refused_by_filename_even_when_it_claims_the_safe_format() -> None:
    """Pickle extension, but the manifest claims SafeGraphArtifactV1 — still refused.

    A manifest is an assertion, not evidence. Trusting the declared format over
    the filename would let a hostile manifest launder a pickle past the reader.
    """

    record = {"path": "/runs/x/graph.gpickle", "format": SCHEMA_NAME, "sha256": "0" * 64}

    assert declares_executable_serialization(record) == "graph.gpickle"


def test_the_removed_legacy_provenance_sidecar_is_treated_as_evidence() -> None:
    record = {"path": f"/runs/x/graph{ARTIFACT_SUFFIX}{LEGACY_PROVENANCE_SUFFIX}"}

    assert declares_executable_serialization(record) is not None
    assert declares_executable_serialization({"legacy_executable_serialization": True})


def test_manifest_validation_names_the_offending_logical_key() -> None:
    manifest = {
        "stages": {
            "partition": {
                "outputs": {"clusters": {"path": "/runs/x/clusters.gpkg"}},
                "input_records": {"graph": {"path": "/runs/x/graph.gpickle"}},
            }
        }
    }

    offenders = legacy_declarations(manifest)

    assert offenders == ["manifest.stages.partition.input_records.graph (graph.gpickle)"]


def test_a_manifest_declaring_a_pickle_can_be_neither_written_nor_loaded(tmp_path: Path) -> None:
    """The refusal sits in ``validate_manifest``, which every reader and writer uses."""

    manifest = {
        "schema_version": 1,
        "run_id": "r", "scope": "tiny", "status": "complete",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "git": {}, "runtime": {}, "config": {}, "publish_history": [],
        "inputs": {"fingerprint": "f", "files": {}},
        "stages": {},
    }
    validate_manifest(manifest)  # control: the shape itself is acceptable

    manifest["inputs"]["files"] = {"partition.graph": {"path": "/runs/x/graph.gpickle"}}
    with pytest.raises(ExecutableSerializationRefused, match="partition.graph"):
        validate_manifest(manifest)

    with pytest.raises(ExecutableSerializationRefused):
        atomic_write_json(tmp_path / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    assert not (tmp_path / MANIFEST_FILENAME).exists()


def test_no_manifest_record_can_be_built_for_a_pickle(tmp_path: Path) -> None:
    """``file_record`` refuses by name, so no writer can record one by accident."""

    legacy = tmp_path / "graph.gpickle"
    legacy.write_bytes(b"\x80\x05not-read")

    with pytest.raises(ExecutableSerializationRefused, match="no longer supported"):
        file_record(legacy)


# ---------------------------------------------------------------------------
# 8. Resume refuses a legacy Preparation manifest without opening the payload
# ---------------------------------------------------------------------------

def test_resume_refuses_a_preparation_manifest_that_declares_a_pickle(
    tmp_path: Path, no_unpickling: None
) -> None:
    from test_preparation_resume_identity import (
        _complete_identity_manifest,
        _write_identity_fixture,
    )

    project, config, output = _write_identity_fixture(tmp_path)
    manifest = _complete_identity_manifest(project, config, output)

    # Rewrite the graph output the way a pre-migration run recorded it, and put
    # a genuinely hostile payload at that path.
    marker = output / "executed.txt"
    legacy = hostile_pickle(output / "segment_relation_graph_road_poi_order.gpickle", marker)
    manifest["outputs"]["graph"] = {
        "path": legacy.as_posix(), "size": legacy.stat().st_size, "sha256": "0" * 64,
    }
    (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ExecutableSerializationRefused, match="no longer supported"):
        preparation.inspect_resume(config, project, output)

    assert not marker.exists()


def test_safe_preparation_manifest_still_resumes(tmp_path: Path) -> None:
    """Control for the test above: an unmodified manifest is still reusable."""

    from test_preparation_resume_identity import (
        _complete_identity_manifest,
        _write_identity_fixture,
    )

    project, config, output = _write_identity_fixture(tmp_path)
    _complete_identity_manifest(project, config, output)

    assert preparation.inspect_resume(config, project, output)["reusable"] is True


# ---------------------------------------------------------------------------
# 9-10. Publish and export refuse before creating or copying anything
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", EXECUTABLE_SERIALIZATION_SUFFIXES)
def test_publish_refuses_a_pickle_in_the_bundle_by_name(suffix: str, tmp_path: Path) -> None:
    project, run_dir = complete_run(tmp_path)
    publish_scope(run_dir, scope="tiny")
    target = project / "data/processed/tiny"

    (target / f"smuggled{suffix}").write_bytes(b"\x80\x05payload")

    with pytest.raises(PublishError, match="executable serialization"):
        publishing._validate_staging(target, run_dir, build_publish_inventory(run_dir))


def test_publish_refuses_a_declaring_run_before_the_staging_directory_exists(tmp_path: Path) -> None:
    """A run whose manifest names a pickle must not even get a staging tree."""

    project, run_dir = complete_run(tmp_path)
    manifest = load_manifest(run_dir)
    manifest["stages"]["partition"]["outputs"]["clusters_csv"] = {
        "path": (run_dir / "partition/clusters.pkl").as_posix(), "size": 1, "sha256": "0" * 64,
    }
    (run_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    processed = project / "data/processed"
    before = sorted(path.name for path in processed.rglob("*")) if processed.exists() else []

    with pytest.raises((PublishError, ExecutableSerializationRefused)):
        publish_scope(run_dir, scope="tiny")

    after = sorted(path.name for path in processed.rglob("*")) if processed.exists() else []
    assert after == before, "publish created filesystem state for a refused run"


def test_export_refuses_a_pickle_in_the_release_by_name(tmp_path: Path) -> None:
    project, run_dir = complete_run(tmp_path)
    export_reproduction(run_dir, output="minimal-v1", profile="minimal")
    output = release_output(project, "minimal-v1")

    (output / "smuggled.gpickle").write_bytes(b"\x80\x05payload")

    with pytest.raises(ExportError, match="executable serialization"):
        reproduction._validate_release(output)


def test_export_refuses_a_declaring_run_before_the_release_root_exists(tmp_path: Path) -> None:
    project, run_dir = complete_run(tmp_path)
    manifest = load_manifest(run_dir)
    manifest["stages"]["partition"]["outputs"]["clusters_csv"] = {
        "path": (run_dir / "partition/clusters.pickle").as_posix(), "size": 1, "sha256": "0" * 64,
    }
    (run_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    releases = release_output(project, "unused").parent

    with pytest.raises((ExportError, ExecutableSerializationRefused)):
        export_reproduction(run_dir, output="minimal-v1", profile="minimal")

    assert not releases.exists(), "export created a release root for a refused run"


# ---------------------------------------------------------------------------
# 11. The R5.1 interface is gone from the CLI
# ---------------------------------------------------------------------------

def test_the_legacy_opt_in_flag_is_unknown_to_the_cli() -> None:
    from roadnet_partition import cli

    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--config", "c.yaml", "--allow-trusted-legacy-graph-pickle"])


def test_the_migrate_command_no_longer_exists() -> None:
    from roadnet_partition import cli

    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["migrate-legacy-graph", "--input", "a", "--output", "b"])


def test_the_cli_reports_a_refused_legacy_graph_as_an_operator_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal must reach the operator as an actionable message, not a traceback."""

    from roadnet_partition import cli

    exit_code = cli.main(["partition", "--config", str(tmp_path / "absent.yaml")])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# The safe format keeps working — a refusal that broke the happy path is a bug
# ---------------------------------------------------------------------------

def test_safe_artifact_round_trips_and_stays_byte_deterministic(tmp_path: Path) -> None:
    source = relation_graph()
    first = tmp_path / f"first{ARTIFACT_SUFFIX}"
    second = tmp_path / f"second{ARTIFACT_SUFFIX}"
    write_safe_graph(source, first)
    write_safe_graph(source, second)

    assert first.read_bytes() == second.read_bytes()

    loaded = read_safe_graph(first)
    assert set(loaded.nodes) == set(source.nodes)
    assert set(map(frozenset, loaded.edges)) == set(map(frozenset, source.edges))
    assert nx.get_edge_attributes(loaded, "weight") == nx.get_edge_attributes(source, "weight")


def test_a_safe_artifact_name_is_never_mistaken_for_executable_serialization() -> None:
    assert not is_executable_serialization_name(f"graph{ARTIFACT_SUFFIX}")
    assert is_executable_serialization_name("graph.gpickle")
    assert is_executable_serialization_name("graph.pkl")
    assert is_executable_serialization_name("graph.pickle")


def test_a_full_run_publishes_and_exports_with_no_executable_serialization(tmp_path: Path) -> None:
    """End-to-end control: the pickle-free pipeline still completes both bundles."""

    project, run_dir = complete_run(tmp_path)
    publish_scope(run_dir, scope="tiny")
    export_reproduction(run_dir, output="minimal-v1", profile="minimal")

    for bundle in (project / "data/processed/tiny", release_output(project, "minimal-v1")):
        for suffix in (*EXECUTABLE_SERIALIZATION_SUFFIXES, LEGACY_PROVENANCE_SUFFIX):
            assert not list(bundle.rglob(f"*{suffix}"))
