"""``SafeGraphArtifactV1`` — non-executable exchange format for relation graphs.

Remediation AUD-005 / batch R5.1. The segment relation graph used to be
exchanged as a Python pickle, which executes arbitrary code during
deserialization, before any validation can run. This module is the single
place where the replacement format is written and read.

The artifact is deterministic gzip-compressed UTF-8 JSON with the extension
``.graph.json.gz``. Only JSON scalars (``str``, ``int``, ``float``, ``bool``)
and ``null`` may appear in node/edge attributes, so no object graph is ever
reconstructed from the file. The reader validates the full payload against the
attribute schema declared inside the artifact *before* it builds a
``networkx.Graph``, and never falls back to pickle.

See ``docs/security/safe-graph-artifact-v1.md`` for the audited contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping
import zlib

import networkx as nx

from roadnet_partition.io.manifests import file_record
from roadnet_partition.io.serialization_policy import (
    LEGACY_UNSUPPORTED_MESSAGE,
    SUPPORTED_GRAPH_FORMAT,
    is_executable_serialization_name,
)


SCHEMA_NAME = SUPPORTED_GRAPH_FORMAT
SCHEMA_VERSION = 1
ARTIFACT_SUFFIX = ".graph.json.gz"
GRAPH_TYPE = "networkx.Graph"
GZIP_COMPRESS_LEVEL = 6
GZIP_MAGIC = b"\x1f\x8b"
PICKLE_MAGIC = b"\x80"

_SCALAR_TAGS = ("bool", "int", "float", "str")
_NODE_ID_TAGS = ("int", "str")
_HEADER_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "graph_type",
        "directed",
        "multigraph",
        "node_id_type",
        "graph_attributes",
        "node_attributes",
        "edge_attributes",
        "node_count",
        "edge_count",
        "nodes",
        "edges",
    }
)


class SafeGraphArtifactError(ValueError):
    """Raised when a graph artifact is missing, malformed, or untrusted."""


@dataclass(frozen=True)
class GraphArtifactMeta:
    """Structural summary of a ``SafeGraphArtifactV1`` payload."""

    format: str
    schema_version: int
    graph_type: str
    node_id_type: str
    node_count: int
    edge_count: int
    semantic_digest: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _type_tag(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    raise SafeGraphArtifactError(f"unsupported attribute type: {type(value).__name__}")


def _coerce_scalar(context: str, value: Any) -> Any:
    """Normalize ``value`` to a JSON scalar, or fail loudly.

    ``NaN`` is mapped to ``None`` because pandas uses it for absent optional
    fields; infinities are rejected because they have no portable JSON
    representation.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            raise SafeGraphArtifactError(f"{context}: non-finite float is not representable")
        return float(value)
    if isinstance(value, str):
        return str(value)
    item = getattr(value, "item", None)
    if callable(item) and getattr(value, "ndim", None) == 0:
        return _coerce_scalar(context, item())
    raise SafeGraphArtifactError(f"{context}: unsupported attribute type {type(value).__name__}")


def _coerce_attributes(context: str, attributes: Mapping[Any, Any]) -> dict[str, Any]:
    coerced: dict[str, Any] = {}
    for key, value in attributes.items():
        if not isinstance(key, str):
            raise SafeGraphArtifactError(f"{context}: attribute name must be a string, got {type(key).__name__}")
        coerced[key] = _coerce_scalar(f"{context}.{key}", value)
    return coerced


def _declare_schema(entries: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Derive the closed attribute schema observed across ``entries``."""

    tags: dict[str, set[str]] = {}
    nullable: dict[str, bool] = {}
    for attributes in entries:
        for key, value in attributes.items():
            observed = tags.setdefault(key, set())
            if value is None:
                nullable[key] = True
                continue
            nullable.setdefault(key, False)
            observed.add(_type_tag(value))
    return {
        key: {"types": sorted(tags[key]), "nullable": bool(nullable.get(key, False))}
        for key in sorted(tags)
    }


def _node_id_type(nodes: Iterable[Any]) -> str:
    tags = set()
    for node in nodes:
        if isinstance(node, bool) or not isinstance(node, (int, str)):
            raise SafeGraphArtifactError(
                f"node identifier must be a string or integer, got {type(node).__name__}"
            )
        tags.add("str" if isinstance(node, str) else "int")
    if len(tags) > 1:
        raise SafeGraphArtifactError("graph mixes string and integer node identifiers; relabel before writing")
    return tags.pop() if tags else "str"


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _build_payload(graph: nx.Graph) -> dict[str, Any]:
    if graph.is_directed() or graph.is_multigraph():
        raise SafeGraphArtifactError(
            f"{SCHEMA_NAME} only supports undirected simple graphs (networkx.Graph)"
        )
    node_id_type = _node_id_type(graph.nodes)
    nodes = [
        [node, _coerce_attributes(f"node[{node!r}]", attributes)]
        for node, attributes in sorted(graph.nodes(data=True), key=lambda item: item[0])
    ]
    edges: list[list[Any]] = []
    for left, right, attributes in graph.edges(data=True):
        if left == right:
            raise SafeGraphArtifactError(f"self-loop on node {left!r} is not part of the relation graph contract")
        first, second = (left, right) if left <= right else (right, left)
        edges.append([first, second, _coerce_attributes(f"edge[{first!r},{second!r}]", attributes)])
    edges.sort(key=lambda item: (item[0], item[1]))
    payload = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "graph_type": GRAPH_TYPE,
        "directed": False,
        "multigraph": False,
        "node_id_type": node_id_type,
        "graph_attributes": _coerce_attributes("graph", graph.graph),
        "node_attributes": _declare_schema(entry[1] for entry in nodes),
        "edge_attributes": _declare_schema(entry[2] for entry in edges),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }
    return payload


def semantic_digest(graph: nx.Graph) -> str:
    """SHA-256 over the canonical projection of ``graph``.

    The digest depends only on node identifiers, edge pairs, and attribute
    values — not on file framing, compression, or write order.
    """

    return hashlib.sha256(_canonical_json(_build_payload(graph))).hexdigest()


def graph_meta(graph: nx.Graph) -> GraphArtifactMeta:
    payload = _build_payload(graph)
    return GraphArtifactMeta(
        format=SCHEMA_NAME,
        schema_version=SCHEMA_VERSION,
        graph_type=GRAPH_TYPE,
        node_id_type=payload["node_id_type"],
        node_count=payload["node_count"],
        edge_count=payload["edge_count"],
        semantic_digest=hashlib.sha256(_canonical_json(payload)).hexdigest(),
    )


def write_safe_graph(graph: nx.Graph, path: str | Path) -> GraphArtifactMeta:
    """Write ``graph`` as a deterministic ``SafeGraphArtifactV1`` artifact.

    Equal graphs always produce byte-identical files. The artifact is written
    to a temporary file in the destination directory and atomically renamed.
    """

    destination = Path(path)
    payload = _build_payload(graph)
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
    payload["semantic_digest"] = digest
    body = _canonical_json(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=GZIP_COMPRESS_LEVEL, mtime=0) as handle:
                handle.write(body)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return GraphArtifactMeta(
        format=SCHEMA_NAME,
        schema_version=SCHEMA_VERSION,
        graph_type=GRAPH_TYPE,
        node_id_type=payload["node_id_type"],
        node_count=payload["node_count"],
        edge_count=payload["edge_count"],
        semantic_digest=digest,
    )


def reject_legacy_graph_path(path: str | Path) -> None:
    """Refuse a legacy graph path by name, before any byte is read.

    Deserializing a pickle executes whatever the file says to execute, so the
    refusal has to happen on the filename, ahead of opening the file.
    """

    source = Path(path)
    if is_executable_serialization_name(source.name):
        raise SafeGraphArtifactError(f"{source}: {LEGACY_UNSUPPORTED_MESSAGE}")


def _reject_constant(name: str) -> Any:
    raise SafeGraphArtifactError(f"artifact contains the non-JSON constant {name}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SafeGraphArtifactError(f"artifact contains a duplicate object key: {key}")
        result[key] = value
    return result


def _read_payload(path: Path) -> dict[str, Any]:
    reject_legacy_graph_path(path)
    if not path.exists():
        raise SafeGraphArtifactError(
            f"graph artifact not found: {path}. Expected a {SCHEMA_NAME} file ending in {ARTIFACT_SUFFIX}."
        )
    if not path.is_file():
        raise SafeGraphArtifactError(f"graph artifact is not a regular file: {path}")
    with path.open("rb") as handle:
        prefix = handle.read(2)
    if prefix != GZIP_MAGIC:
        if prefix[:1] == PICKLE_MAGIC:
            raise SafeGraphArtifactError(
                f"{path} is a Python pickle, not a {SCHEMA_NAME} artifact; "
                f"refusing to deserialize executable data. {LEGACY_UNSUPPORTED_MESSAGE}"
            )
        raise SafeGraphArtifactError(f"{path} is not a gzip-compressed {SCHEMA_NAME} artifact")
    try:
        body = gzip.decompress(path.read_bytes())
    except (OSError, EOFError, zlib.error) as error:
        raise SafeGraphArtifactError(f"{path} is not readable as gzip: {error}") from error
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SafeGraphArtifactError(f"{path} is not valid UTF-8: {error}") from error
    try:
        payload = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as error:
        raise SafeGraphArtifactError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise SafeGraphArtifactError(f"{path} must contain a JSON object at the top level")
    return payload


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SafeGraphArtifactError(message)


def _validate_schema_declaration(label: str, declaration: Any) -> dict[str, dict[str, Any]]:
    _require(isinstance(declaration, dict), f"{label} must be a JSON object")
    validated: dict[str, dict[str, Any]] = {}
    for key, entry in declaration.items():
        _require(isinstance(entry, dict), f"{label}.{key} must be a JSON object")
        _require(set(entry) == {"types", "nullable"}, f"{label}.{key} must declare exactly 'types' and 'nullable'")
        types = entry["types"]
        _require(isinstance(types, list), f"{label}.{key}.types must be a list")
        _require(
            all(isinstance(tag, str) and tag in _SCALAR_TAGS for tag in types),
            f"{label}.{key}.types must only contain {list(_SCALAR_TAGS)}",
        )
        _require(isinstance(entry["nullable"], bool), f"{label}.{key}.nullable must be a boolean")
        validated[key] = {"types": list(types), "nullable": entry["nullable"]}
    return validated


def _validate_attributes(
    label: str,
    attributes: Any,
    declaration: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    _require(isinstance(attributes, dict), f"{label} attributes must be a JSON object")
    validated: dict[str, Any] = {}
    for key, value in attributes.items():
        entry = declaration.get(key)
        _require(entry is not None, f"{label} declares undeclared attribute {key!r}")
        if value is None:
            _require(entry["nullable"], f"{label}.{key} is null but the schema declares it non-nullable")
            validated[key] = None
            continue
        _require(
            not isinstance(value, (dict, list)),
            f"{label}.{key} must be a JSON scalar, got {type(value).__name__}",
        )
        tag = _type_tag(value)
        _require(
            tag in entry["types"],
            f"{label}.{key} has type {tag} which is not declared in {entry['types']}",
        )
        validated[key] = value
    return validated


def read_safe_graph(
    path: str | Path,
    *,
    expected_semantic_digest: str | None = None,
) -> nx.Graph:
    """Read a ``SafeGraphArtifactV1`` artifact into a ``networkx.Graph``.

    The payload is fully validated before any graph object is constructed.
    This function never calls ``pickle``, ``eval``, ``yaml.load``, or
    ``marshal``, and never falls back to another format.
    """

    graph, _ = read_safe_graph_with_meta(path, expected_semantic_digest=expected_semantic_digest)
    return graph


def read_safe_graph_with_meta(
    path: str | Path,
    *,
    expected_semantic_digest: str | None = None,
) -> tuple[nx.Graph, GraphArtifactMeta]:
    source = Path(path)
    payload = _read_payload(source)

    unknown = set(payload) - _HEADER_KEYS - {"semantic_digest"}
    _require(not unknown, f"{source} contains unknown top-level keys: {sorted(unknown)}")
    missing = _HEADER_KEYS - set(payload)
    _require(not missing, f"{source} is missing required keys: {sorted(missing)}")

    _require(
        payload["schema"] == SCHEMA_NAME,
        f"{source} declares schema {payload['schema']!r}, expected {SCHEMA_NAME!r}",
    )
    _require(
        payload["schema_version"] == SCHEMA_VERSION,
        f"{source} declares schema_version {payload['schema_version']!r}, expected {SCHEMA_VERSION}",
    )
    _require(
        payload["graph_type"] == GRAPH_TYPE,
        f"{source} declares graph_type {payload['graph_type']!r}, expected {GRAPH_TYPE!r}",
    )
    _require(payload["directed"] is False, f"{source} declares a directed graph, which is unsupported")
    _require(payload["multigraph"] is False, f"{source} declares a multigraph, which is unsupported")
    node_id_type = payload["node_id_type"]
    _require(
        node_id_type in _NODE_ID_TAGS,
        f"{source} declares node_id_type {node_id_type!r}, expected one of {list(_NODE_ID_TAGS)}",
    )

    node_schema = _validate_schema_declaration("node_attributes", payload["node_attributes"])
    edge_schema = _validate_schema_declaration("edge_attributes", payload["edge_attributes"])
    raw_graph_attributes = payload["graph_attributes"]
    _require(isinstance(raw_graph_attributes, dict), f"{source}: graph_attributes must be a JSON object")
    graph_attributes = _validate_attributes(
        "graph_attributes",
        raw_graph_attributes,
        _declare_schema([raw_graph_attributes]),
    )

    raw_nodes = payload["nodes"]
    raw_edges = payload["edges"]
    _require(isinstance(raw_nodes, list), f"{source}: nodes must be a list")
    _require(isinstance(raw_edges, list), f"{source}: edges must be a list")
    _require(
        payload["node_count"] == len(raw_nodes),
        f"{source}: node_count {payload['node_count']!r} does not match {len(raw_nodes)} node entries",
    )
    _require(
        payload["edge_count"] == len(raw_edges),
        f"{source}: edge_count {payload['edge_count']!r} does not match {len(raw_edges)} edge entries",
    )

    nodes: list[tuple[Any, dict[str, Any]]] = []
    seen_nodes: set[Any] = set()
    for index, entry in enumerate(raw_nodes):
        _require(isinstance(entry, list) and len(entry) == 2, f"{source}: nodes[{index}] must be [id, attributes]")
        node = entry[0]
        _require(
            _matches_id_type(node, node_id_type),
            f"{source}: nodes[{index}] identifier is not of declared type {node_id_type}",
        )
        _require(node not in seen_nodes, f"{source}: duplicate node identifier {node!r}")
        seen_nodes.add(node)
        nodes.append((node, _validate_attributes(f"{source}: nodes[{index}]", entry[1], node_schema)))

    edges: list[tuple[Any, Any, dict[str, Any]]] = []
    seen_edges: set[tuple[Any, Any]] = set()
    for index, entry in enumerate(raw_edges):
        _require(
            isinstance(entry, list) and len(entry) == 3,
            f"{source}: edges[{index}] must be [source, target, attributes]",
        )
        left, right = entry[0], entry[1]
        for endpoint in (left, right):
            _require(
                _matches_id_type(endpoint, node_id_type),
                f"{source}: edges[{index}] endpoint is not of declared type {node_id_type}",
            )
            _require(endpoint in seen_nodes, f"{source}: edges[{index}] references unknown node {endpoint!r}")
        _require(left != right, f"{source}: edges[{index}] is a self-loop on {left!r}")
        pair = (left, right) if left <= right else (right, left)
        _require(pair not in seen_edges, f"{source}: duplicate edge {pair!r}")
        seen_edges.add(pair)
        edges.append((left, right, _validate_attributes(f"{source}: edges[{index}]", entry[2], edge_schema)))

    graph = nx.Graph()
    graph.graph.update(graph_attributes)
    for node, attributes in nodes:
        graph.add_node(node, **attributes)
    for left, right, attributes in edges:
        graph.add_edge(left, right, **attributes)

    recomputed = semantic_digest(graph)
    declared = payload.get("semantic_digest")
    _require(
        isinstance(declared, str) and len(declared) == 64,
        f"{source} is missing a valid semantic_digest",
    )
    _require(
        recomputed == declared,
        f"{source}: semantic digest mismatch (declared {declared}, recomputed {recomputed})",
    )
    if expected_semantic_digest is not None and expected_semantic_digest != recomputed:
        raise SafeGraphArtifactError(
            f"{source}: semantic digest {recomputed} does not match the expected digest {expected_semantic_digest}"
        )
    meta = GraphArtifactMeta(
        format=SCHEMA_NAME,
        schema_version=SCHEMA_VERSION,
        graph_type=GRAPH_TYPE,
        node_id_type=node_id_type,
        node_count=len(nodes),
        edge_count=len(edges),
        semantic_digest=recomputed,
    )
    return graph, meta


def _matches_id_type(node: Any, node_id_type: str) -> bool:
    if isinstance(node, bool):
        return False
    if node_id_type == "str":
        return isinstance(node, str)
    return isinstance(node, int)


def is_safe_graph_artifact(path: str | Path) -> bool:
    """Return ``True`` when ``path`` parses as a valid ``SafeGraphArtifactV1``."""

    try:
        read_safe_graph(path)
    except SafeGraphArtifactError:
        return False
    return True


def artifact_record(path: str | Path, meta: GraphArtifactMeta) -> dict[str, Any]:
    """Manifest record combining file integrity and graph structure fields."""

    return {**file_record(path), **meta.as_dict()}
