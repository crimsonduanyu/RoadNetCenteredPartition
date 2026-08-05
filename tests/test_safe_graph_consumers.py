"""Gate D regression tests: every public graph consumer reads only SafeGraphArtifactV1.

AUD-005 remediation. The relation graph used to travel between stages as a
``pickle``, so any writer of ``outputs/preparation/*.gpickle`` could execute
arbitrary code inside Partition, Evaluation, and the best-partition figure.
These tests pin the migrated behaviour: the three public loaders share one safe
reader, hostile payloads are rejected before a graph object exists, and no
formal output is produced when the artifact cannot be trusted.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import pickle
from typing import Any, Callable

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString

from roadnet_partition.config import ResolvedStageConfig
from roadnet_partition.io.safe_graph import (
    ARTIFACT_SUFFIX,
    SCHEMA_VERSION,
    SafeGraphArtifactError,
    graph_meta,
    read_safe_graph,
    write_safe_graph,
)
from roadnet_partition.pipeline.preparation import output_paths as preparation_output_paths
from roadnet_partition.pipeline.results import RunContext, StageStatus
from roadnet_partition.reporting import best_partition_map
from roadnet_partition.zoning import evaluate, partition

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The three public graph paths named by AUD-005.
CONSUMERS: dict[str, Any] = {
    "partition": partition,
    "evaluate": evaluate,
    "best_partition_map": best_partition_map,
}

CONSUMER_SOURCES = [
    "src/roadnet_partition/zoning/partition.py",
    "src/roadnet_partition/zoning/evaluate.py",
    "src/roadnet_partition/reporting/best_partition_map.py",
    "scripts/figures/best_partition_maps.py",
    "scripts/figures/partition_order_panels.py",
]


def relation_graph() -> nx.Graph:
    graph = nx.Graph()
    for left, right, weight in [("a", "b", 5.0), ("b", "c", 1.0), ("c", "d", 5.0)]:
        graph.add_edge(
            left, right, weight=weight,
            continuity_weight=weight, connector_weight=weight,
        )
    for node in graph.nodes:
        graph.nodes[node]["length"] = 1.0
    return graph


def safe_artifact(root: Path, graph: nx.Graph | None = None) -> Path:
    path = root / f"graph{ARTIFACT_SUFFIX}"
    write_safe_graph(graph if graph is not None else relation_graph(), path)
    return path


def payload_of(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def rewrite(path: Path, payload: dict[str, Any]) -> Path:
    with gzip.GzipFile(filename="", mode="wb", fileobj=path.open("wb"), mtime=0) as handle:
        handle.write(json.dumps(payload).encode("utf-8"))
    return path


class Marker:
    """Pickle payload whose reconstruction hook writes a file on load."""

    def __init__(self, marker: Path) -> None:
        self.marker = marker

    def __reduce__(self) -> tuple[Callable[..., Any], tuple[Any, ...]]:
        return (Path.write_text, (self.marker, "arbitrary code executed"))


# ---------------------------------------------------------------------------
# Hostile artifacts. Each builder returns a path the consumer must refuse.
# ---------------------------------------------------------------------------

def _missing(root: Path) -> Path:
    return root / f"absent{ARTIFACT_SUFFIX}"


def _plain_pickle(root: Path) -> Path:
    path = root / f"pickled{ARTIFACT_SUFFIX}"
    path.write_bytes(pickle.dumps(relation_graph()))
    return path


def _gzipped_pickle(root: Path) -> Path:
    path = root / f"gzipped{ARTIFACT_SUFFIX}"
    with gzip.GzipFile(filename="", mode="wb", fileobj=path.open("wb"), mtime=0) as handle:
        handle.write(pickle.dumps(relation_graph()))
    return path


def _not_gzip(root: Path) -> Path:
    path = root / f"plain{ARTIFACT_SUFFIX}"
    path.write_text(json.dumps({"format": "SafeGraphArtifactV1"}), encoding="utf-8")
    return path


def _truncated(root: Path) -> Path:
    path = safe_artifact(root)
    payload = path.read_bytes()
    path.write_bytes(payload[: len(payload) // 2])
    return path


def _future_schema(root: Path) -> Path:
    path = safe_artifact(root)
    payload = payload_of(path)
    payload["schema_version"] = SCHEMA_VERSION + 1
    return rewrite(path, payload)


def _directed(root: Path) -> Path:
    path = safe_artifact(root)
    payload = payload_of(path)
    payload["directed"] = True
    return rewrite(path, payload)


def _dropped_node(root: Path) -> Path:
    """``nodes`` no longer matches the declared ``node_count``."""
    path = safe_artifact(root)
    payload = payload_of(path)
    payload["nodes"] = payload["nodes"][:-1]
    return rewrite(path, payload)


def _dangling_edge(root: Path) -> Path:
    path = safe_artifact(root)
    payload = payload_of(path)
    payload["edges"][0][0] = "not-a-node"
    return rewrite(path, payload)


def _undeclared_attribute(root: Path) -> Path:
    path = safe_artifact(root)
    payload = payload_of(path)
    payload["edges"][0][2]["injected"] = "surprise"
    return rewrite(path, payload)


HOSTILE_ARTIFACTS: list[tuple[str, Callable[[Path], Path]]] = [
    ("missing_graph", _missing),
    ("pickle_masquerading_as_artifact", _plain_pickle),
    ("gzipped_pickle", _gzipped_pickle),
    ("not_gzip", _not_gzip),
    ("truncated", _truncated),
    ("wrong_schema_version", _future_schema),
    ("directed_graph_type", _directed),
    ("dropped_node", _dropped_node),
    ("dangling_edge", _dangling_edge),
    ("undeclared_attribute", _undeclared_attribute),
]


# ---------------------------------------------------------------------------
# Shared safe reader
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_public_consumers_load_the_safe_artifact(name: str, tmp_path: Path) -> None:
    source = relation_graph()
    loaded = CONSUMERS[name].load_graph(safe_artifact(tmp_path, source))

    assert isinstance(loaded, nx.Graph)
    assert not loaded.is_directed() and not loaded.is_multigraph()
    assert set(loaded.nodes) == set(source.nodes)
    assert set(map(frozenset, loaded.edges)) == set(map(frozenset, source.edges))
    assert nx.get_edge_attributes(loaded, "weight") == nx.get_edge_attributes(source, "weight")
    assert nx.get_node_attributes(loaded, "length") == nx.get_node_attributes(source, "length")


@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_public_consumers_still_relabel_integer_nodes_to_strings(name: str, tmp_path: Path) -> None:
    """Node-identifier coercion is a pre-existing contract; the format swap keeps it."""
    integer_graph = nx.Graph()
    integer_graph.add_edge(1, 2, weight=1.0)

    loaded = CONSUMERS[name].load_graph(safe_artifact(tmp_path, integer_graph))

    assert set(loaded.nodes) == {"1", "2"}
    assert all(isinstance(node, str) for node in loaded.nodes)


@pytest.mark.parametrize("name", sorted(CONSUMERS))
@pytest.mark.parametrize(
    ("label", "build"), HOSTILE_ARTIFACTS, ids=[label for label, _ in HOSTILE_ARTIFACTS]
)
def test_public_consumers_reject_untrusted_artifacts(
    name: str, label: str, build: Callable[[Path], Path], tmp_path: Path
) -> None:
    root = tmp_path / f"{name}-{label}"
    root.mkdir()

    with pytest.raises(SafeGraphArtifactError):
        CONSUMERS[name].load_graph(build(root))


def test_shared_reader_rejects_a_semantic_digest_mismatch(tmp_path: Path) -> None:
    """Consumers delegate to this reader, so digest enforcement lives here.

    A payload can be internally consistent yet still be the wrong graph. The
    manifest records ``semantic_digest`` (Gate C), and any caller that knows the
    expected value gets an integrity check on top of the structural validation.
    """
    path = safe_artifact(tmp_path)
    tampered = payload_of(path)
    tampered["edges"][0][2]["weight"] = 999.0
    rewrite(path, tampered)

    expected = graph_meta(relation_graph()).semantic_digest
    with pytest.raises(SafeGraphArtifactError, match="digest"):
        read_safe_graph(path, expected_semantic_digest=expected)


@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_public_consumers_never_execute_pickle_payloads(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The AUD-005 reproduction: a __reduce__ hook must never fire."""
    root = tmp_path / name
    root.mkdir()
    marker = root / "arbitrary-code-executed.txt"
    hostile = root / f"hostile{ARTIFACT_SUFFIX}"
    hostile.write_bytes(pickle.dumps(Marker(marker)))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"{name}.load_graph reached pickle deserialization")

    monkeypatch.setattr(pickle, "load", forbidden)
    monkeypatch.setattr(pickle, "loads", forbidden)

    with pytest.raises(SafeGraphArtifactError):
        CONSUMERS[name].load_graph(hostile)
    assert not marker.exists()

    # Same payload behind gzip framing, so the magic-byte guard is not the only defence.
    wrapped = root / f"hostile-gz{ARTIFACT_SUFFIX}"
    with gzip.GzipFile(filename="", mode="wb", fileobj=wrapped.open("wb"), mtime=0) as handle:
        handle.write(pickle.dumps(Marker(marker)))
    with pytest.raises(SafeGraphArtifactError):
        CONSUMERS[name].load_graph(wrapped)
    assert not marker.exists()


# ---------------------------------------------------------------------------
# Static guarantees on the migrated modules and their script entrypoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("relative", CONSUMER_SOURCES)
def test_public_consumer_sources_carry_no_pickle_or_legacy_paths(relative: str) -> None:
    source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")

    for forbidden in ("import pickle", "pickle.load", "pickle.Unpickler", "marshal.", "gpickle"):
        assert forbidden not in source, f"{relative} still references {forbidden}"


@pytest.mark.parametrize(
    "relative", ["scripts/figures/best_partition_maps.py", "scripts/figures/partition_order_panels.py"]
)
def test_figure_scripts_resolve_the_graph_from_preparation_outputs(relative: str) -> None:
    """Figure entrypoints must not restate the artifact filename."""
    source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")

    assert "preparation_output_paths" in source
    assert "segment_relation_graph" not in source


def test_preparation_publishes_only_the_safe_artifact_name() -> None:
    assert preparation_output_paths(Path("/tmp/preparation"))["graph"].name.endswith(ARTIFACT_SUFFIX)


# ---------------------------------------------------------------------------
# No partial formal output when the artifact is untrusted
# ---------------------------------------------------------------------------

def partition_config(tmp_path: Path, graph_path: Path) -> ResolvedStageConfig:
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    segments = gpd.GeoDataFrame(
        {"seg_id": ["a", "b", "c", "d"], "length": [1.0, 1.0, 1.0, 1.0]},
        geometry=[LineString([(index, 0), (index + 1, 0)]) for index in range(4)],
        crs="EPSG:3857",
    )
    segment_path = inputs / "segments.gpkg"
    segments.to_file(segment_path, driver="GPKG")
    baseline_path = inputs / "baseline.gpkg"
    segments.assign(cluster_id=[0, 0, 1, 1]).to_file(baseline_path, driver="GPKG")
    order_path = inputs / "orders.csv"
    pd.DataFrame({"seg_id": ["a", "b", "c", "d"], "order_total": [1, 1, 5, 5]}).to_csv(order_path, index=False)
    relation_path = inputs / "relations.csv"
    pd.DataFrame({"seg_id_a": ["a"], "seg_id_b": ["b"]}).to_csv(relation_path, index=False)

    config_path = tmp_path / "configs" / "partition.yaml"
    config_path.parent.mkdir(exist_ok=True)
    return ResolvedStageConfig(
        config_path,
        {
            "scope": {"active": "tiny", "graph_variant": "road"},
            "inputs": {
                "graph": graph_path,
                "relation_edges": relation_path,
                "segment_nodes": segment_path,
                "order_features": order_path,
                "baseline_clusters": {"leiden": baseline_path},
            },
            "outputs": {"root": "ignored", "overwrite": True, "resume": False},
            "initializations": ["leiden"],
            "objective": {
                "target_clusters": 2,
                "capacity_loss": "squared_hinge",
                "capacity_min_ratio": 0.5,
                "capacity_max_ratio": 1.5,
                "lambda_g": 1.0,
                "lambda_r": 1.0,
                "alpha_cont": 1.0,
                "alpha_conn": 1.0,
                "grid": {"lambda_c": [1.0]},
            },
            "search": {
                "max_passes": 1,
                "min_delta": 1.0e-9,
                "move_policy": "best_improving",
                "enforce_connectivity": True,
                "allow_merge_split": False,
                "grid": {"merge_split_enabled": [False]},
            },
            "evaluation": {},
        },
        "fingerprint",
    )


def test_partition_stage_writes_no_clusters_when_the_graph_is_a_pickle(tmp_path: Path) -> None:
    graph_path = _plain_pickle(tmp_path)
    context = RunContext("tiny", tmp_path / "external-run", tmp_path).for_stage("partition")

    with pytest.raises(SafeGraphArtifactError):
        partition.run_partition(partition_config(tmp_path, graph_path), context)

    produced = sorted(path.name for path in context.stage_dir.rglob("*") if path.is_file())
    assert not [name for name in produced if name.endswith((".gpkg", ".csv"))], produced


def test_partition_stage_completes_on_the_safe_artifact(tmp_path: Path) -> None:
    """Control case: the same fixture succeeds once the artifact is the safe format."""
    context = RunContext("tiny", tmp_path / "external-run", tmp_path).for_stage("partition")

    result = partition.run_partition(partition_config(tmp_path, safe_artifact(tmp_path)), context)

    assert result.status is StageStatus.COMPLETE
    assert "manifest" in result.outputs
