from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
import pickle
import struct
from typing import Any, Callable

import networkx as nx
import pytest

from roadnet_partition.io import safe_graph
from roadnet_partition.io.safe_graph import (
    ARTIFACT_SUFFIX,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    SafeGraphArtifactError,
    artifact_record,
    graph_meta,
    is_safe_graph_artifact,
    read_safe_graph,
    read_safe_graph_with_meta,
    semantic_digest,
    write_safe_graph,
)


def sample_graph() -> nx.Graph:
    graph = nx.Graph()
    graph.add_node("s2", u=2, v=3, highway="primary", name="Second Ring", osmid=222, length=180.5, bearing=90.0, segment_role="ordinary")
    graph.add_node("s1", u=1, v=2, highway="trunk", name="北四环", osmid=111, length=120.25, bearing=12.5, segment_role="ordinary")
    graph.add_node("s3", u=3, v=4, highway="secondary", name=None, osmid=None, length=95.0, bearing=270.0, segment_role="ordinary")
    graph.add_edge(
        "s1",
        "s2",
        relation_types="continuity|direct",
        has_direct=True,
        has_continuity=True,
        has_connector=False,
        same_osmid=False,
        same_name=False,
        same_highway=False,
        direct_weight=1.0,
        continuity_weight=0.5,
        connector_weight=0.0,
        poi_weight=0.25,
        order_weight=0.125,
        base_weight=1.5,
        weight=1.875,
        continuity_score=0.5,
        poi_similarity=0.5,
        order_similarity=0.25,
        angle_diff=12.5,
        connector_count=0,
        connector_ids="",
        connector_highways="",
    )
    graph.add_edge(
        "s2",
        "s3",
        relation_types="connector",
        has_direct=False,
        has_continuity=False,
        has_connector=True,
        same_osmid=False,
        same_name=False,
        same_highway=False,
        direct_weight=0.0,
        continuity_weight=0.0,
        connector_weight=0.75,
        poi_weight=0.0,
        order_weight=0.0,
        base_weight=0.75,
        weight=0.75,
        continuity_score=0.0,
        poi_similarity=0.0,
        order_similarity=0.0,
        angle_diff=None,
        connector_count=1,
        connector_ids="c9",
        connector_highways="service",
    )
    return graph


def write_payload(path: Path, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with gzip.GzipFile(filename=str(path), mode="wb", compresslevel=6, mtime=0) as handle:
        handle.write(body)


def read_payload(path: Path) -> dict[str, Any]:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def tampered(tmp_path: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    source = tmp_path / f"source{ARTIFACT_SUFFIX}"
    write_safe_graph(sample_graph(), source)
    payload = read_payload(source)
    mutate(payload)
    target = tmp_path / f"tampered{ARTIFACT_SUFFIX}"
    write_payload(target, payload)
    return target


def test_round_trip_preserves_nodes_edges_and_attribute_types(tmp_path: Path) -> None:
    original = sample_graph()
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"
    write_safe_graph(original, path)

    restored = read_safe_graph(path)

    assert type(restored) is nx.Graph
    assert not restored.is_directed() and not restored.is_multigraph()
    assert set(restored.nodes) == set(original.nodes)
    assert {frozenset(edge) for edge in restored.edges} == {frozenset(edge) for edge in original.edges}
    for node, attributes in original.nodes(data=True):
        assert restored.nodes[node] == attributes
        for key, value in attributes.items():
            assert type(restored.nodes[node][key]) is type(value)
    for left, right, attributes in original.edges(data=True):
        assert restored.edges[left, right] == attributes
        for key, value in attributes.items():
            assert type(restored.edges[left, right][key]) is type(value)


def test_round_trip_preserves_integer_node_identifiers(tmp_path: Path) -> None:
    graph = nx.Graph()
    graph.add_node(2, length=1.0)
    graph.add_node(10, length=2.0)
    graph.add_edge(2, 10, weight=1.0)
    path = tmp_path / f"int{ARTIFACT_SUFFIX}"

    meta = write_safe_graph(graph, path)
    restored = read_safe_graph(path)

    assert meta.node_id_type == "int"
    assert sorted(restored.nodes) == [2, 10]
    assert all(type(node) is int for node in restored.nodes)


def test_writer_is_byte_deterministic_regardless_of_insertion_order(tmp_path: Path) -> None:
    forward = tmp_path / f"forward{ARTIFACT_SUFFIX}"
    reversed_order = tmp_path / f"reversed{ARTIFACT_SUFFIX}"
    write_safe_graph(sample_graph(), forward)

    shuffled = nx.Graph()
    original = sample_graph()
    for node in reversed(list(original.nodes)):
        shuffled.add_node(node, **original.nodes[node])
    for left, right, attributes in reversed(list(original.edges(data=True))):
        shuffled.add_edge(right, left, **attributes)
    write_safe_graph(shuffled, reversed_order)

    assert forward.read_bytes() == reversed_order.read_bytes()
    assert semantic_digest(original) == semantic_digest(shuffled)


def test_writer_emits_a_fixed_gzip_header_timestamp(tmp_path: Path) -> None:
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"
    write_safe_graph(sample_graph(), path)

    header = path.read_bytes()[:10]
    assert header[:2] == b"\x1f\x8b"
    assert struct.unpack("<I", header[4:8])[0] == 0


def test_writer_leaves_no_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"
    write_safe_graph(sample_graph(), path)

    assert [entry.name for entry in tmp_path.iterdir()] == [path.name]


def test_semantic_digest_changes_when_an_attribute_changes(tmp_path: Path) -> None:
    graph = sample_graph()
    before = semantic_digest(graph)
    graph.edges["s1", "s2"]["weight"] = 1.876

    assert semantic_digest(graph) != before


def test_meta_and_manifest_record_describe_the_artifact(tmp_path: Path) -> None:
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"
    meta = write_safe_graph(sample_graph(), path)

    assert meta == graph_meta(sample_graph())
    assert meta.format == SCHEMA_NAME
    assert meta.schema_version == SCHEMA_VERSION
    assert meta.graph_type == "networkx.Graph"
    assert (meta.node_count, meta.edge_count) == (3, 2)

    record = artifact_record(path, meta)
    assert set(record) == {
        "path", "size", "sha256",
        "format", "schema_version", "graph_type", "node_id_type",
        "node_count", "edge_count", "semantic_digest",
    }
    assert record["size"] == path.stat().st_size
    assert record["semantic_digest"] == meta.semantic_digest


def test_reader_returns_matching_meta(tmp_path: Path) -> None:
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"
    written = write_safe_graph(sample_graph(), path)

    _, meta = read_safe_graph_with_meta(path, expected_semantic_digest=written.semantic_digest)

    assert meta == written


def test_reader_rejects_an_unexpected_semantic_digest(tmp_path: Path) -> None:
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"
    write_safe_graph(sample_graph(), path)

    with pytest.raises(SafeGraphArtifactError, match="does not match the expected digest"):
        read_safe_graph(path, expected_semantic_digest="0" * 64)


def test_nan_attributes_are_normalized_to_null(tmp_path: Path) -> None:
    graph = sample_graph()
    graph.nodes["s1"]["name"] = math.nan
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"

    write_safe_graph(graph, path)

    assert read_safe_graph(path).nodes["s1"]["name"] is None


def test_writer_rejects_unsupported_graphs_and_values(tmp_path: Path) -> None:
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"

    with pytest.raises(SafeGraphArtifactError, match="undirected simple graphs"):
        write_safe_graph(nx.DiGraph([("a", "b")]), path)
    with pytest.raises(SafeGraphArtifactError, match="undirected simple graphs"):
        write_safe_graph(nx.MultiGraph([("a", "b")]), path)

    self_loop = nx.Graph()
    self_loop.add_edge("a", "a")
    with pytest.raises(SafeGraphArtifactError, match="self-loop"):
        write_safe_graph(self_loop, path)

    infinite = nx.Graph()
    infinite.add_node("a", length=math.inf)
    with pytest.raises(SafeGraphArtifactError, match="non-finite float"):
        write_safe_graph(infinite, path)

    nested = nx.Graph()
    nested.add_node("a", payload={"unsafe": True})
    with pytest.raises(SafeGraphArtifactError, match="unsupported attribute type dict"):
        write_safe_graph(nested, path)

    mixed = nx.Graph()
    mixed.add_edge("a", 1)
    with pytest.raises(SafeGraphArtifactError, match="mixes string and integer node identifiers"):
        write_safe_graph(mixed, path)

    assert not path.exists()


def test_reader_rejects_a_pickle_masquerading_as_an_artifact(tmp_path: Path) -> None:
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"
    path.write_bytes(pickle.dumps(sample_graph()))

    with pytest.raises(SafeGraphArtifactError, match="is a Python pickle"):
        read_safe_graph(path)
    assert not is_safe_graph_artifact(path)


def test_reader_rejects_a_gzipped_pickle(tmp_path: Path) -> None:
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"
    path.write_bytes(gzip.compress(pickle.dumps(sample_graph())))

    with pytest.raises(SafeGraphArtifactError, match="not valid UTF-8|not valid JSON"):
        read_safe_graph(path)


def test_reader_rejects_missing_and_non_gzip_and_truncated_files(tmp_path: Path) -> None:
    missing = tmp_path / f"missing{ARTIFACT_SUFFIX}"
    with pytest.raises(SafeGraphArtifactError, match="graph artifact not found"):
        read_safe_graph(missing)

    with pytest.raises(SafeGraphArtifactError, match="not a regular file"):
        read_safe_graph(tmp_path)

    plain = tmp_path / f"plain{ARTIFACT_SUFFIX}"
    plain.write_text("{}", encoding="utf-8")
    with pytest.raises(SafeGraphArtifactError, match="not a gzip-compressed"):
        read_safe_graph(plain)

    truncated = tmp_path / f"truncated{ARTIFACT_SUFFIX}"
    write_safe_graph(sample_graph(), truncated)
    truncated.write_bytes(truncated.read_bytes()[:-16])
    with pytest.raises(SafeGraphArtifactError, match="not readable as gzip"):
        read_safe_graph(truncated)


def test_reader_rejects_invalid_json_and_non_json_constants(tmp_path: Path) -> None:
    broken = tmp_path / f"broken{ARTIFACT_SUFFIX}"
    broken.write_bytes(gzip.compress(b"{not json"))
    with pytest.raises(SafeGraphArtifactError, match="not valid JSON"):
        read_safe_graph(broken)

    scalar = tmp_path / f"scalar{ARTIFACT_SUFFIX}"
    scalar.write_bytes(gzip.compress(b"[]"))
    with pytest.raises(SafeGraphArtifactError, match="JSON object at the top level"):
        read_safe_graph(scalar)

    nan_payload = tmp_path / f"nan{ARTIFACT_SUFFIX}"
    nan_payload.write_bytes(gzip.compress(b'{"schema": NaN}'))
    with pytest.raises(SafeGraphArtifactError, match="non-JSON constant NaN"):
        read_safe_graph(nan_payload)

    duplicated = tmp_path / f"duplicated{ARTIFACT_SUFFIX}"
    duplicated.write_bytes(gzip.compress(b'{"schema": "a", "schema": "b"}'))
    with pytest.raises(SafeGraphArtifactError, match="duplicate object key"):
        read_safe_graph(duplicated)


def _duplicate_node(payload: dict[str, Any]) -> None:
    payload["nodes"].append(payload["nodes"][0])
    payload["node_count"] += 1


def _duplicate_edge(payload: dict[str, Any]) -> None:
    payload["edges"].append(payload["edges"][0])
    payload["edge_count"] += 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.__setitem__("schema", "SafeGraphArtifactV2"), "declares schema"),
        (lambda payload: payload.__setitem__("schema_version", 2), "declares schema_version"),
        (lambda payload: payload.__setitem__("graph_type", "networkx.DiGraph"), "declares graph_type"),
        (lambda payload: payload.__setitem__("directed", True), "declares a directed graph"),
        (lambda payload: payload.__setitem__("multigraph", True), "declares a multigraph"),
        (lambda payload: payload.__setitem__("node_id_type", "tuple"), "declares node_id_type"),
        (lambda payload: payload.__setitem__("node_count", 99), "node_count"),
        (lambda payload: payload.__setitem__("edge_count", 99), "edge_count"),
        (lambda payload: payload.__setitem__("surprise", 1), "unknown top-level keys"),
        (lambda payload: payload.pop("nodes"), "missing required keys"),
        (lambda payload: payload.pop("semantic_digest"), "missing a valid semantic_digest"),
        (lambda payload: payload.__setitem__("semantic_digest", "a" * 64), "semantic digest mismatch"),
        (lambda payload: payload.__setitem__("graph_attributes", []), "graph_attributes must be a JSON object"),
        (lambda payload: payload["nodes"][0].__setitem__(0, 7), "identifier is not of declared type"),
        (lambda payload: payload["nodes"][0][1].__setitem__("evil", "x"), "undeclared attribute"),
        (lambda payload: payload["nodes"][0][1].__setitem__("length", "long"), "which is not declared"),
        (lambda payload: payload["nodes"][0][1].__setitem__("length", None), "non-nullable"),
        (lambda payload: payload["nodes"][0][1].__setitem__("length", {"$": 1}), "must be a JSON scalar"),
        (_duplicate_node, "duplicate node identifier"),
        (lambda payload: payload["nodes"].__setitem__(0, ["s1"]), "must be \\[id, attributes\\]"),
        (lambda payload: payload["edges"][0].__setitem__(1, "ghost"), "references unknown node"),
        (lambda payload: payload["edges"][0].__setitem__(1, payload["edges"][0][0]), "self-loop"),
        (_duplicate_edge, "duplicate edge"),
        (lambda payload: payload["edges"].__setitem__(0, ["s1", "s2"]), "must be \\[source, target, attributes\\]"),
        (lambda payload: payload["edges"][0][2].__setitem__("weight", "heavy"), "which is not declared"),
        (lambda payload: payload["node_attributes"].__setitem__("length", {"types": ["float"]}), "exactly 'types' and 'nullable'"),
        (lambda payload: payload["node_attributes"]["length"].__setitem__("types", ["complex"]), "must only contain"),
        (lambda payload: payload["node_attributes"]["length"].__setitem__("nullable", "yes"), "nullable must be a boolean"),
        (lambda payload: payload.__setitem__("node_attributes", 5), "node_attributes must be a JSON object"),
    ],
)
def test_reader_rejects_tampered_payloads(tmp_path: Path, mutate, message: str) -> None:
    path = tampered(tmp_path, mutate)

    with pytest.raises(SafeGraphArtifactError, match=message):
        read_safe_graph(path)


def test_reader_never_deserializes_python_objects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / f"graph{ARTIFACT_SUFFIX}"
    write_safe_graph(sample_graph(), path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the safe graph reader must never unpickle data")

    monkeypatch.setattr(pickle, "load", forbidden)
    monkeypatch.setattr(pickle, "loads", forbidden)
    monkeypatch.setattr(pickle, "Unpickler", forbidden)

    assert read_safe_graph(path).number_of_nodes() == 3


def test_safe_graph_module_contains_no_executable_deserialization() -> None:
    source = Path(safe_graph.__file__).read_text(encoding="utf-8")
    body = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("#", '"""')))

    for forbidden in ("pickle.load", "pickle.Unpickler", "marshal.", "yaml.load(", "eval(", "exec("):
        assert forbidden not in body, f"{forbidden} must not appear in the safe graph module"
