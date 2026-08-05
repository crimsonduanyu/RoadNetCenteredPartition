from __future__ import annotations

from contextlib import redirect_stdout
import gzip
import io
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
import yaml

from roadnet_partition.io.manifests import atomic_write_json, file_record, input_fingerprint, load_manifest, validate_manifest
from roadnet_partition.io.safe_graph import (
    ARTIFACT_SUFFIX,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    read_safe_graph,
    semantic_digest,
    write_safe_graph,
)
from roadnet_partition.pipeline import preparation
from roadnet_partition.pipeline.preparation import PreparationIdentityError
from roadnet_partition.pipeline.runner import resolve_pipeline_config, run_pipeline
from roadnet_partition.pipeline.stages import STAGE_ORDER
from roadnet_partition.pipeline.validation import validate_run
from test_pipeline_runner import write_full_fixture


INPUT_NAMES = ("raw_edges", "boundary", "ring_segments", "poi", "zoning_orders")


def _write_identity_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    inputs = project / "inputs"
    inputs.mkdir(parents=True)
    for name in INPUT_NAMES:
        (inputs / f"{name}.dat").write_bytes(f"{name}-aaaa".encode())
    dataset = project / "dataset.yaml"
    dataset.write_text(yaml.safe_dump({"crs": {}, "study_area": {}, "tag": "aaaa"}), encoding="utf-8")
    config = project / "preparation.yaml"
    config.write_text(yaml.safe_dump({
        "schema_version": 1,
        "dataset_config": "dataset.yaml",
        "inputs": {name: f"inputs/{name}.dat" for name in INPUT_NAMES},
        "connector_rules": {"max_connector_length_m": 120},
    }, sort_keys=False), encoding="utf-8")
    return project, config, tmp_path / "run/preparation"


def _tiny_graph() -> nx.Graph:
    graph = nx.Graph()
    graph.add_node("s1", length=1.0)
    graph.add_node("s2", length=2.0)
    graph.add_edge("s1", "s2", weight=1.0)
    return graph


def _complete_identity_manifest(project: Path, config: Path, output: Path) -> dict:
    _, identity = preparation.preparation_identity(config, project)
    output.mkdir(parents=True)
    paths = preparation.output_paths(output)
    for name, path in paths.items():
        if name == preparation.GRAPH_OUTPUT_NAME:
            write_safe_graph(_tiny_graph(), path)
        else:
            path.write_bytes(f"output-{name}".encode())
    manifest = {
        "schema_version": preparation.PREPARATION_MANIFEST_SCHEMA_VERSION,
        "status": "complete",
        "identity": identity,
        "config": identity["config"],
        "inputs": identity["inputs"],
        "outputs": {name: preparation.output_record(name, path) for name, path in paths.items()},
    }
    atomic_write_json(output / "manifest.json", manifest)
    return manifest


def test_unchanged_identity_reuses_without_rewriting_outputs(tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    manifest = _complete_identity_manifest(project, config, output)
    before = {name: path.stat().st_mtime_ns for name, path in preparation.output_paths(output).items()}
    capture = io.StringIO()

    with redirect_stdout(capture):
        returned = preparation.run(config, project, output)

    assert capture.getvalue().strip() == "preparation: reused"
    assert {name: path.stat().st_mtime_ns for name, path in returned.items()} == before
    assert preparation.inspect_resume(config, project, output)["reason"] == "preparation_identity_match"
    assert preparation.inspect_resume(config, project, output)["current_identity"] == manifest["identity"]


def test_config_bytes_change_invalidates_even_when_path_is_unchanged(tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    stored = _complete_identity_manifest(project, config, output)["identity"]
    values = yaml.safe_load(config.read_text(encoding="utf-8"))
    values["connector_rules"]["max_connector_length_m"] = 121
    config.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")

    decision = preparation.inspect_resume(config, project, output)

    assert decision["reason"] == "preparation_config_changed"
    assert decision["current_identity"]["config"]["sha256"] != stored["config"]["sha256"]


def test_semantically_equivalent_yaml_byte_rewrite_invalidates_by_contract(tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    _complete_identity_manifest(project, config, output)
    values = yaml.safe_load(config.read_text(encoding="utf-8"))
    config.write_text(yaml.safe_dump(values, sort_keys=True), encoding="utf-8")
    assert preparation.inspect_resume(config, project, output)["reason"] == "preparation_config_changed"


@pytest.mark.parametrize("name", INPUT_NAMES)
def test_same_size_input_byte_change_invalidates_each_real_input(name: str, tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    stored = _complete_identity_manifest(project, config, output)["identity"]
    path = project / "inputs" / f"{name}.dat"
    old = path.read_bytes()
    path.write_bytes(old[:-1] + (b"b" if old[-1:] != b"b" else b"c"))

    decision = preparation.inspect_resume(config, project, output)

    logical = f"preparation.{name}"
    assert decision["reason"] == f"preparation_input_changed: {logical}"
    assert path.stat().st_size == stored["inputs"][logical]["size"]
    assert decision["current_identity"]["inputs"][logical]["sha256"] != stored["inputs"][logical]["sha256"]


def test_dataset_config_byte_change_is_an_input_identity_change(tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    stored = _complete_identity_manifest(project, config, output)["identity"]
    dataset = project / "dataset.yaml"
    old = dataset.read_bytes()
    dataset.write_bytes(old.replace(b"aaaa", b"bbbb"))

    decision = preparation.inspect_resume(config, project, output)

    logical = "preparation.dataset_config"
    assert dataset.stat().st_size == len(old)
    assert decision["reason"] == f"preparation_input_changed: {logical}"
    assert decision["current_identity"]["inputs"][logical]["sha256"] != stored["inputs"][logical]["sha256"]


def test_input_record_order_does_not_change_digest(tmp_path: Path) -> None:
    project, config, _ = _write_identity_fixture(tmp_path)
    _, identity = preparation.preparation_identity(config, project)
    reversed_identity = {
        "schema_version": 1,
        "config": identity["config"],
        "inputs": dict(reversed(list(identity["inputs"].items()))),
    }
    assert input_fingerprint(reversed_identity) == identity["digest"]


def test_missing_input_fails_before_output_write(tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    marker = tmp_path / "marker.bin"
    marker.write_bytes(b"unchanged")
    (project / "inputs/poi.dat").unlink()

    with pytest.raises(PreparationIdentityError, match="preparation_input_missing: preparation.poi"):
        preparation.run(config, project, output)

    assert marker.read_bytes() == b"unchanged"
    assert not output.exists()


@pytest.mark.parametrize("failure", ["invalid_config", "hash_error"])
def test_identity_failure_occurs_before_reuse_or_write(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    marker = tmp_path / "marker.bin"
    marker.write_bytes(b"unchanged")
    if failure == "invalid_config":
        config.write_text("[]\n", encoding="utf-8")
    else:
        original = preparation.file_record

        def fail_hash(path):
            if Path(path).name == "poi.dat":
                raise OSError("synthetic hash read failure")
            return original(path)

        monkeypatch.setattr(preparation, "file_record", fail_hash)

    with pytest.raises((ValueError, OSError)):
        preparation.run(config, project, output)

    assert marker.read_bytes() == b"unchanged"
    assert not output.exists()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("missing_output", "preparation_output_missing"),
        ("changed_output", "preparation_output_changed"),
        ("unexpected_output", "preparation_output_changed"),
        ("output_inventory", "preparation_manifest_incomplete"),
        ("incomplete", "preparation_manifest_incomplete"),
        ("legacy_config", "preparation_identity_missing_legacy_manifest"),
        ("legacy_inputs", "preparation_identity_missing_legacy_manifest"),
        ("legacy_digest", "preparation_identity_missing_legacy_manifest"),
    ],
)
def test_output_and_legacy_state_never_reuses(change: str, reason: str, tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    manifest = _complete_identity_manifest(project, config, output)
    if change == "missing_output":
        preparation.output_paths(output)["graph"].unlink()
    elif change == "changed_output":
        preparation.output_paths(output)["graph"].write_bytes(b"tampered")
    elif change == "unexpected_output":
        (output / "unexpected.bin").write_bytes(b"unexpected")
    elif change == "output_inventory":
        manifest["outputs"].pop("graph")
    elif change == "incomplete":
        manifest["status"] = "running"
    else:
        if change == "legacy_config":
            manifest["identity"].pop("config")
        elif change == "legacy_inputs":
            manifest["identity"].pop("inputs")
        else:
            manifest["identity"].pop("digest")
    if change not in {"missing_output", "changed_output", "unexpected_output"}:
        atomic_write_json(output / "manifest.json", manifest)

    decision = preparation.inspect_resume(config, project, output)

    assert decision["reusable"] is False
    assert decision["reason"].startswith(reason)


def test_graph_output_record_carries_safe_artifact_fields(tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    manifest = _complete_identity_manifest(project, config, output)

    record = manifest["outputs"][preparation.GRAPH_OUTPUT_NAME]

    assert record["format"] == SCHEMA_NAME
    assert record["schema_version"] == SCHEMA_VERSION
    assert record["graph_type"] == "networkx.Graph"
    assert record["node_id_type"] == "str"
    assert (record["node_count"], record["edge_count"]) == (2, 1)
    assert record["semantic_digest"] == semantic_digest(_tiny_graph())
    assert record["size"] == preparation.output_paths(output)["graph"].stat().st_size
    assert preparation.inspect_resume(config, project, output)["reusable"] is True


def test_semantically_changed_graph_never_reuses_even_with_a_refreshed_file_record(tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    manifest = _complete_identity_manifest(project, config, output)
    path = preparation.output_paths(output)["graph"]
    replacement = _tiny_graph()
    replacement.edges["s1", "s2"]["weight"] = 2.0
    write_safe_graph(replacement, path)
    manifest["outputs"]["graph"] = {**manifest["outputs"]["graph"], **file_record(path)}
    atomic_write_json(output / "manifest.json", manifest)

    decision = preparation.inspect_resume(config, project, output)

    assert decision["reusable"] is False
    assert decision["reason"] == "preparation_output_changed: graph"


def test_unreadable_graph_artifact_never_reuses(tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    manifest = _complete_identity_manifest(project, config, output)
    path = preparation.output_paths(output)["graph"]
    path.write_bytes(gzip.compress(b'{"schema": "SafeGraphArtifactV1"}'))
    manifest["outputs"]["graph"] = {**manifest["outputs"]["graph"], **file_record(path)}
    atomic_write_json(output / "manifest.json", manifest)

    decision = preparation.inspect_resume(config, project, output)

    assert preparation.output_record("graph", path) is None
    assert decision["reusable"] is False
    assert decision["reason"] == "preparation_output_changed: graph"


def test_missing_or_malformed_preparation_manifest_never_reuses(tmp_path: Path) -> None:
    project, config, output = _write_identity_fixture(tmp_path)
    _complete_identity_manifest(project, config, output)
    manifest = output / "manifest.json"
    manifest.unlink()
    assert preparation.inspect_resume(config, project, output)["reason"] == "preparation_manifest_incomplete"
    manifest.write_text("[\n", encoding="utf-8")
    assert preparation.inspect_resume(config, project, output)["reason"] == "preparation_manifest_incomplete"


def test_preparation_run_emits_only_a_safe_graph_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    pipeline_path = write_full_fixture(project)
    config_path = _add_preparation_config(project, pipeline_path)
    _fake_preparation_algorithms(monkeypatch)
    output = tmp_path / "preparation"

    paths = preparation.run(config_path, project, output)

    assert paths["graph"].name == f"segment_relation_graph_road_poi_order{ARTIFACT_SUFFIX}"
    assert paths["graph"].read_bytes()[:2] == b"\x1f\x8b"
    assert not list(output.rglob("*.gpickle")) and not list(output.rglob("*.pkl"))
    manifest = yaml.safe_load((output / "manifest.json").read_text(encoding="utf-8"))
    record = manifest["outputs"]["graph"]
    assert record["format"] == SCHEMA_NAME
    assert record["semantic_digest"] == semantic_digest(read_safe_graph(paths["graph"]))
    assert preparation.inspect_resume(config_path, project, output)["reusable"] is True


def test_recomputation_removes_unexpected_owned_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    pipeline_path = write_full_fixture(project)
    config_path = _add_preparation_config(project, pipeline_path)
    _fake_preparation_algorithms(monkeypatch)
    output = tmp_path / "preparation"
    preparation.run(config_path, project, output)
    unexpected = output / "unexpected"
    unexpected.mkdir()
    (unexpected / "artifact.bin").write_bytes(b"unexpected")

    decision = preparation.inspect_resume(config_path, project, output)
    preparation.run(config_path, project, output, inspection=decision)

    assert not unexpected.exists()
    assert preparation.inspect_resume(config_path, project, output)["reusable"] is True


def _add_preparation_config(project: Path, pipeline_path: Path) -> Path:
    prep = project / "configs/preparation/tiny.yaml"
    prep.parent.mkdir(parents=True)
    prep.write_text(yaml.safe_dump({
        "schema_version": 1,
        "dataset_config": "../datasets/tiny.yaml",
        "inputs": {
            "raw_edges": "../../inputs/partition/segments.gpkg",
            "boundary": "../../inputs/partition/segments.gpkg",
            "ring_segments": "../../inputs/partition/segments.gpkg",
            "poi": "../../inputs/partition/poi.csv",
            "zoning_orders": "../../inputs/partition/orders.csv",
        },
        "connector_rules": {"max_connector_length_m": 120},
        "baseline": {"algorithm": "leiden", "resolution": 0.6, "random_state": 42},
    }, sort_keys=False), encoding="utf-8")
    pipeline = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))
    pipeline["preparation"] = {"config": "../preparation/tiny.yaml"}
    pipeline_path.write_text(yaml.safe_dump(pipeline, sort_keys=False), encoding="utf-8")
    return prep


def _fake_preparation_algorithms(monkeypatch: pytest.MonkeyPatch) -> None:
    def roads(config, paths):
        frame = gpd.read_file(config["inputs"]["raw_edges"]).copy()
        frame["segment_role"] = "ordinary"
        frame.to_file(paths["classified_edges"], driver="GPKG")
        frame.to_file(paths["segment_nodes"], driver="GPKG")
        return frame

    def poi(config, paths, _ordinary):
        frame = pd.read_csv(config["inputs"]["poi"])
        frame.to_csv(paths["poi_features"], index=False)
        pd.DataFrame({"category_col": [], "poi_type": []}).to_csv(paths["poi_category_mapping"], index=False)
        return frame

    def orders(config, paths, _ordinary):
        frame = pd.read_csv(config["inputs"]["zoning_orders"])
        frame.to_csv(paths["order_features"], index=False)
        frame.to_csv(paths["order_od_pairs"], index=False)
        frame.to_csv(paths["hourly_od"], index=False)
        return frame

    def graph(config, paths, *_args):
        relations = pd.read_csv(config["inputs"]["zoning_orders"].parent / "relations.csv")
        relations["base_weight"] = 1.0
        relations.to_csv(paths["relation_edges"], index=False)
        source = read_safe_graph(config["inputs"]["raw_edges"].parent / f"graph{ARTIFACT_SUFFIX}")
        return source, write_safe_graph(source, paths["graph"])

    monkeypatch.setattr(preparation, "_preprocess_roads", roads)
    monkeypatch.setattr(preparation, "_build_poi_features", poi)
    monkeypatch.setattr(preparation, "_build_order_features", orders)
    monkeypatch.setattr(preparation, "_build_relation_graph", graph)
    monkeypatch.setattr(preparation, "run_leiden", lambda graph, _config: {
        node: 0 if index < 2 else 1 for index, node in enumerate(graph.nodes)
    })


@pytest.mark.parametrize(("isolate", "change"), [(False, "config"), (True, "input")])
def test_pipeline_preparation_mismatch_invalidates_all_stages_before_reexecution(
    isolate: bool,
    change: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    pipeline_path = write_full_fixture(project)
    prep_path = _add_preparation_config(project, pipeline_path)
    _fake_preparation_algorithms(monkeypatch)
    config = resolve_pipeline_config(pipeline_path)
    run_dir = tmp_path / "run"
    run_pipeline(config, run_dir=run_dir, isolate_stages=isolate)
    capsys.readouterr()
    before = {
        name: path.stat().st_mtime_ns
        for name, path in preparation.output_paths(run_dir / "preparation").items()
    }
    run_pipeline(config, run_dir=run_dir, resume=True, isolate_stages=isolate)
    unchanged_output = capsys.readouterr().out
    assert "preparation: reused" in unchanged_output
    assert all(f"{stage}: reused" in unchanged_output for stage in STAGE_ORDER)
    assert {
        name: path.stat().st_mtime_ns
        for name, path in preparation.output_paths(run_dir / "preparation").items()
    } == before
    other_run = tmp_path / "other-run"
    other_run.mkdir()
    marker = other_run / "marker.bin"
    marker.write_bytes(b"other")
    if change == "config":
        values = yaml.safe_load(prep_path.read_text(encoding="utf-8"))
        values["connector_rules"]["max_connector_length_m"] = 121
        prep_path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        expected_reason = "preparation_config_changed"
    else:
        source = project / "inputs/partition/poi.csv"
        old = source.read_bytes()
        source.write_bytes(old.replace(b",1", b",2", 1))
        assert source.stat().st_size == len(old)
        expected_reason = "preparation_input_changed: preparation.poi"

    updated = resolve_pipeline_config(pipeline_path)
    run_pipeline(
        updated, run_dir=run_dir, from_stage="supply", to_stage="tte",
        resume=True, isolate_stages=isolate,
    )
    output = capsys.readouterr().out
    manifest = load_manifest(run_dir)
    decision = manifest["pipeline"]["preparation"]["last_decision"]

    assert "preparation: reused" not in output
    assert f"preparation: invalidated ({expected_reason})" in output
    assert [manifest["stages"][stage]["status"] for stage in STAGE_ORDER] == ["complete"] * 4
    assert decision["reason"] == expected_reason
    assert decision["invalidated_stages"] == list(STAGE_ORDER)
    assert decision["recomputation_performed"] is True
    assert all(f"{stage}: reused" not in output for stage in STAGE_ORDER)
    assert marker.read_bytes() == b"other"
    assert manifest["pipeline"]["config_fingerprint"] != manifest["pipeline"]["base_config_fingerprint"]
    assert manifest["pipeline"]["config_fingerprint"] == manifest["config"]["fingerprint"]
    assert validate_run(run_dir, write_report=False)["overall_status"] == "passed"


def test_legacy_pipeline_and_preparation_manifest_recompute_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    pipeline_path = write_full_fixture(project)
    _add_preparation_config(project, pipeline_path)
    _fake_preparation_algorithms(monkeypatch)
    config = resolve_pipeline_config(pipeline_path)
    run_dir = tmp_path / "run"
    run_pipeline(config, run_dir=run_dir, to_stage="partition", isolate_stages=False)
    capsys.readouterr()
    manifest = load_manifest(run_dir)
    manifest["config"]["fingerprint"] = config.fingerprint
    manifest["config"]["resolved"] = config.values
    manifest["pipeline"]["config_fingerprint"] = config.fingerprint
    manifest["pipeline"].pop("base_config_fingerprint")
    manifest["pipeline"].pop("preparation")
    manifest["schema_version"] = 1
    manifest.pop("experiment")
    atomic_write_json(run_dir / "manifest.json", manifest, validator=validate_manifest)
    prep_manifest_path = run_dir / "preparation/manifest.json"
    prep_manifest = yaml.safe_load(prep_manifest_path.read_text(encoding="utf-8"))
    prep_manifest["schema_version"] = 1
    prep_manifest.pop("identity")
    prep_manifest.pop("status")
    atomic_write_json(prep_manifest_path, prep_manifest)

    run_pipeline(config, run_dir=run_dir, to_stage="partition", resume=True, isolate_stages=False)
    output = capsys.readouterr().out
    decision = load_manifest(run_dir)["pipeline"]["preparation"]["last_decision"]

    assert "preparation: reused" not in output
    assert "preparation: invalidated (preparation_identity_missing_legacy_manifest)" in output
    assert decision["reason"] == "preparation_identity_missing_legacy_manifest"
    assert decision["invalidated_stages"] == list(STAGE_ORDER)
    assert decision["recomputation_performed"] is True
