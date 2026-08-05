"""Gate E regression tests: legacy pickle reading is a single, closed door.

AUD-005. After the migration the only module in ``src/`` permitted to call
``pickle.load`` is ``io/trusted_legacy_graph_pickle.py``, and it refuses unless
the caller supplies an explicit declaration *and* the operator passes
``--allow-trusted-legacy-graph-pickle``. These tests pin that the door stays
shut by default, that the CLI is the only entrypoint that can open it, and that
opening it is recorded.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import pickle
from typing import Any, Callable

import networkx as nx
import pytest

from roadnet_partition import cli
from roadnet_partition.io.safe_graph import (
    ARTIFACT_SUFFIX,
    SCHEMA_NAME,
    read_safe_graph,
    semantic_digest,
)
from roadnet_partition.io import trusted_legacy_graph_pickle
from roadnet_partition.io.trusted_legacy_graph_pickle import (
    ALLOWED_GLOBALS,
    LEGACY_SUFFIX,
    OPT_IN_FLAG,
    PROVENANCE_KEY,
    TrustedLegacyGraphPickleError,
    convert_trusted_legacy_graph,
    declare_legacy_source,
    load_trusted_legacy_graph,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src/roadnet_partition"
LOADER_RELATIVE = "src/roadnet_partition/io/trusted_legacy_graph_pickle.py"
REASON = "produced by this project before the safe-artifact migration"


def relation_graph() -> nx.Graph:
    graph = nx.Graph()
    graph.add_node("s1", length=120.25, highway="trunk", segment_role="ordinary")
    graph.add_node("s2", length=180.5, highway="primary", segment_role="ordinary")
    graph.add_node("s3", length=95.0, highway="secondary", segment_role="connector")
    graph.add_edge("s1", "s2", weight=1.875, continuity_weight=0.5, has_direct=True)
    graph.add_edge("s2", "s3", weight=0.75, continuity_weight=0.25, has_direct=False)
    return graph


def legacy_pickle(root: Path, payload: Any = None, name: str = "graph") -> Path:
    path = root / f"{name}{LEGACY_SUFFIX}"
    path.write_bytes(pickle.dumps(relation_graph() if payload is None else payload))
    return path


class Marker:
    """Pickle whose reconstruction hook writes a file — the AUD-005 payload."""

    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self) -> tuple[Callable[..., Any], tuple[Any, ...]]:
        return (Path.write_text, (self.marker, "arbitrary code executed"))


# ---------------------------------------------------------------------------
# The door is shut by default
# ---------------------------------------------------------------------------

def test_loading_is_refused_without_the_opt_in(tmp_path: Path) -> None:
    declaration = declare_legacy_source(legacy_pickle(tmp_path), REASON)

    with pytest.raises(TrustedLegacyGraphPickleError, match="disabled by default"):
        load_trusted_legacy_graph(declaration)


def test_default_refusal_happens_before_the_file_is_even_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = declare_legacy_source(legacy_pickle(tmp_path), REASON)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("default path touched pickle deserialization")

    monkeypatch.setattr(pickle, "load", forbidden)
    monkeypatch.setattr(pickle, "loads", forbidden)
    monkeypatch.setattr(trusted_legacy_graph_pickle, "_RestrictedUnpickler", forbidden)

    with pytest.raises(TrustedLegacyGraphPickleError):
        load_trusted_legacy_graph(declaration, allow_trusted_legacy_graph_pickle=False)


def test_conversion_is_refused_without_the_opt_in(tmp_path: Path) -> None:
    declaration = declare_legacy_source(legacy_pickle(tmp_path), REASON)
    destination = tmp_path / f"converted{ARTIFACT_SUFFIX}"

    with pytest.raises(TrustedLegacyGraphPickleError):
        convert_trusted_legacy_graph(declaration, destination)
    assert not destination.exists()


# ---------------------------------------------------------------------------
# The declaration gate
# ---------------------------------------------------------------------------

def test_declaration_rejects_a_non_legacy_suffix(tmp_path: Path) -> None:
    with pytest.raises(TrustedLegacyGraphPickleError, match="safe reader"):
        declare_legacy_source(tmp_path / f"graph{ARTIFACT_SUFFIX}", REASON)


@pytest.mark.parametrize("reason", ["", "   ", "short"])
def test_declaration_requires_a_substantive_trust_reason(tmp_path: Path, reason: str) -> None:
    with pytest.raises(TrustedLegacyGraphPickleError, match="trust reason"):
        declare_legacy_source(legacy_pickle(tmp_path), reason)


def test_declaration_alone_does_not_open_the_door(tmp_path: Path) -> None:
    """Holding a valid declaration is necessary but not sufficient."""
    declaration = declare_legacy_source(legacy_pickle(tmp_path), REASON)
    assert declaration.reason == REASON

    with pytest.raises(TrustedLegacyGraphPickleError):
        load_trusted_legacy_graph(declaration)


# ---------------------------------------------------------------------------
# With both gates open: bounded, warned, and correct
# ---------------------------------------------------------------------------

def test_opted_in_load_returns_the_graph_and_warns(tmp_path: Path, capsys: Any) -> None:
    source = relation_graph()
    declaration = declare_legacy_source(legacy_pickle(tmp_path), REASON)

    graph = load_trusted_legacy_graph(declaration, allow_trusted_legacy_graph_pickle=True)

    assert semantic_digest(graph) == semantic_digest(source)
    stderr = capsys.readouterr().err
    assert "executable serialization" in stderr
    assert REASON in stderr
    assert f"{PROVENANCE_KEY}=true" in stderr


def test_opted_in_load_still_blocks_a_code_execution_payload(tmp_path: Path) -> None:
    """The restricted unpickler bounds the blast radius even inside the door."""
    marker = tmp_path / "arbitrary-code-executed.txt"
    hostile = legacy_pickle(tmp_path, payload=Marker(marker), name="hostile")
    declaration = declare_legacy_source(hostile, REASON)

    with pytest.raises(TrustedLegacyGraphPickleError, match="disallowed global"):
        load_trusted_legacy_graph(declaration, allow_trusted_legacy_graph_pickle=True)
    assert not marker.exists()


@pytest.mark.parametrize("payload", [{"not": "a graph"}, [1, 2, 3], "plain string"])
def test_opted_in_load_rejects_non_graph_payloads(tmp_path: Path, payload: Any) -> None:
    declaration = declare_legacy_source(legacy_pickle(tmp_path, payload=payload), REASON)

    with pytest.raises(TrustedLegacyGraphPickleError, match="not a networkx.Graph"):
        load_trusted_legacy_graph(declaration, allow_trusted_legacy_graph_pickle=True)


def test_missing_source_is_reported_not_crashed(tmp_path: Path) -> None:
    declaration = declare_legacy_source(tmp_path / f"absent{LEGACY_SUFFIX}", REASON)

    with pytest.raises(TrustedLegacyGraphPickleError, match="not found"):
        load_trusted_legacy_graph(declaration, allow_trusted_legacy_graph_pickle=True)


def test_allowlist_excludes_the_usual_execution_primitives() -> None:
    for dangerous in [
        ("os", "system"), ("subprocess", "Popen"), ("builtins", "eval"),
        ("builtins", "exec"), ("builtins", "getattr"), ("builtins", "__import__"),
        ("posix", "system"), ("pathlib", "Path"),
    ]:
        assert dangerous not in ALLOWED_GLOBALS


# ---------------------------------------------------------------------------
# Conversion preserves the graph and records provenance
# ---------------------------------------------------------------------------

def test_conversion_round_trips_the_graph_unchanged(tmp_path: Path) -> None:
    source = relation_graph()
    declaration = declare_legacy_source(legacy_pickle(tmp_path), REASON)
    destination = tmp_path / f"converted{ARTIFACT_SUFFIX}"

    meta, provenance = convert_trusted_legacy_graph(
        declaration, destination, allow_trusted_legacy_graph_pickle=True
    )
    restored = read_safe_graph(destination)

    assert dict(restored.nodes(data=True)) == dict(source.nodes(data=True))
    assert {frozenset(edge): data for *edge, data in restored.edges(data=True)} == {
        frozenset(edge): data for *edge, data in source.edges(data=True)
    }
    assert semantic_digest(restored) == semantic_digest(source)
    assert meta.format == SCHEMA_NAME
    assert meta.node_count == source.number_of_nodes()
    assert meta.edge_count == source.number_of_edges()
    assert provenance[PROVENANCE_KEY] is True
    assert provenance["trust_reason"] == REASON
    assert provenance["source"]["format"] == "python_pickle"
    assert provenance["converted_to"]["semantic_digest"] == meta.semantic_digest


def test_conversion_refuses_to_overwrite(tmp_path: Path) -> None:
    declaration = declare_legacy_source(legacy_pickle(tmp_path), REASON)
    destination = tmp_path / f"converted{ARTIFACT_SUFFIX}"
    destination.write_bytes(b"existing")

    with pytest.raises(TrustedLegacyGraphPickleError, match="overwrite"):
        convert_trusted_legacy_graph(declaration, destination, allow_trusted_legacy_graph_pickle=True)
    assert destination.read_bytes() == b"existing"


# ---------------------------------------------------------------------------
# CLI: the only entrypoint, and it is closed by default
# ---------------------------------------------------------------------------

def test_cli_refuses_without_the_flag(tmp_path: Path, capsys: Any) -> None:
    source = legacy_pickle(tmp_path)
    destination = tmp_path / f"converted{ARTIFACT_SUFFIX}"

    code = cli.main([
        "migrate-legacy-graph",
        "--input", str(source), "--output", str(destination),
        "--trusted-reason", REASON,
    ])

    assert code == 2
    assert "refused" in capsys.readouterr().err
    assert not destination.exists()


def test_cli_converts_and_writes_a_provenance_record(tmp_path: Path, capsys: Any) -> None:
    source = legacy_pickle(tmp_path)
    destination = tmp_path / f"converted{ARTIFACT_SUFFIX}"

    code = cli.main([
        "migrate-legacy-graph",
        "--input", str(source), "--output", str(destination),
        "--trusted-reason", REASON, OPT_IN_FLAG,
    ])

    assert code == 0
    assert semantic_digest(read_safe_graph(destination)) == semantic_digest(relation_graph())
    record_path = destination.with_name(f"{destination.name}.legacy-provenance.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record[PROVENANCE_KEY] is True
    assert record["trust_reason"] == REASON
    captured = capsys.readouterr()
    assert "executable serialization" in captured.err
    assert str(record_path) in captured.out


def test_cli_refuses_a_hostile_payload_even_with_the_flag(tmp_path: Path) -> None:
    marker = tmp_path / "arbitrary-code-executed.txt"
    source = legacy_pickle(tmp_path, payload=Marker(marker), name="hostile")
    destination = tmp_path / f"converted{ARTIFACT_SUFFIX}"

    code = cli.main([
        "migrate-legacy-graph",
        "--input", str(source), "--output", str(destination),
        "--trusted-reason", REASON, OPT_IN_FLAG,
    ])

    assert code == 2
    assert not marker.exists()
    assert not destination.exists()


def test_flag_is_absent_from_every_pipeline_subcommand() -> None:
    """Opting in must never ride along with a normal run."""
    parser = cli.build_parser()
    actions = {action.dest: action for action in parser._actions}
    assert "allow_trusted_legacy_graph_pickle" not in actions

    for command in ("run", "partition", "demand", "supply", "tte", "publish", "export-reproduction"):
        namespace = parser.parse_args(_minimal_args(command))
        assert not hasattr(namespace, "allow_trusted_legacy_graph_pickle"), command


def _minimal_args(command: str) -> list[str]:
    if command == "run":
        return ["run", "--config", "c.yaml"]
    if command == "publish":
        return ["publish", "--run", "r", "--scope", "s"]
    if command == "export-reproduction":
        return ["export-reproduction", "--run", "r", "--output", "o"]
    return [command, "--config", "c.yaml"]


# ---------------------------------------------------------------------------
# Static containment
# ---------------------------------------------------------------------------

def test_the_loader_is_the_only_pickle_call_site_in_the_package() -> None:
    offenders = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative == LOADER_RELATIVE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".")[0] in {"pickle", "marshal", "shelve"} for alias in node.names):
                    offenders.append(relative)
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in {"pickle", "marshal", "shelve"}:
                    offenders.append(relative)
    assert not offenders, sorted(set(offenders))


def test_the_loader_is_not_imported_by_any_graph_consumer() -> None:
    """Importing the loader is how a stage would regress; no stage may do it."""
    consumers = [
        "src/roadnet_partition/pipeline/preparation.py",
        "src/roadnet_partition/zoning/partition.py",
        "src/roadnet_partition/zoning/evaluate.py",
        "src/roadnet_partition/reporting/best_partition_map.py",
        "scripts/figures/best_partition_maps.py",
        "scripts/figures/partition_order_panels.py",
    ]
    for relative in consumers:
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        assert "trusted_legacy_graph_pickle" not in source, relative
