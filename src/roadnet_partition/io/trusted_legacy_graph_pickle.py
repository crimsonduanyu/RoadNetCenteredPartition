"""Trusted-only reader for the pre-AUD-005 relation-graph pickle.

This is the **only** module in ``src/`` allowed to call ``pickle.load``. Every
default graph path — Preparation, Partition, Evaluation, and the best-partition
figure — reads :mod:`roadnet_partition.io.safe_graph` instead, and refuses a
pickle outright.

Loading a pickle executes whatever the file says to execute. This module
therefore exists for exactly one bounded job: letting an operator convert a
``.gpickle`` produced by a *pre-migration run of this project* into a
``SafeGraphArtifactV1`` artifact, without re-running Preparation. It is not a
consumer path, and nothing imports it during a normal run.

Two independent gates must both be open before a byte is deserialized:

1. an explicit :class:`LegacyGraphDeclaration` naming the source and stating why
   it is trusted — no caller can pass a legacy path by accident; and
2. ``allow_trusted_legacy_graph_pickle=True``, which the CLI only sets when the
   operator passes ``--allow-trusted-legacy-graph-pickle``.

Even with both gates open the read is narrowed: a restricted unpickler refuses
any global outside a small graph-shaped allowlist, so a hostile file cannot
reach ``os.system`` or ``builtins.eval`` through this door. That reduces the
blast radius; it does not make an untrusted pickle safe. Only point this at a
file you produced yourself.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import pickle
import sys
from typing import Any, TextIO

import networkx as nx

from roadnet_partition.io.safe_graph import (
    GraphArtifactMeta,
    graph_meta,
    write_safe_graph,
)

LEGACY_SUFFIX = ".gpickle"
OPT_IN_FLAG = "--allow-trusted-legacy-graph-pickle"
PROVENANCE_KEY = "legacy_executable_serialization"
PROVENANCE_SCHEMA = "TrustedLegacyGraphConversionV1"

#: Globals a relation-graph pickle legitimately needs. Everything else is refused.
ALLOWED_GLOBALS: frozenset[tuple[str, str]] = frozenset(
    {
        ("builtins", "dict"),
        ("builtins", "frozenset"),
        ("builtins", "list"),
        ("builtins", "set"),
        ("builtins", "tuple"),
        ("collections", "OrderedDict"),
        ("collections", "defaultdict"),
        ("copyreg", "_reconstructor"),
        ("networkx.classes.graph", "Graph"),
        ("numpy", "dtype"),
        ("numpy", "ndarray"),
        ("numpy.core.multiarray", "_reconstruct"),
        ("numpy.core.multiarray", "scalar"),
    }
)


class TrustedLegacyGraphPickleError(RuntimeError):
    """Raised when a legacy pickle read is refused, or yields a wrong object."""


@dataclass(frozen=True)
class LegacyGraphDeclaration:
    """An explicit, auditable statement that ``source`` is trusted local input."""

    source: Path
    reason: str


def declare_legacy_source(source: str | Path, reason: str) -> LegacyGraphDeclaration:
    """Build the declaration gate, rejecting vague or mistargeted inputs."""
    path = Path(source)
    if path.suffix != LEGACY_SUFFIX:
        raise TrustedLegacyGraphPickleError(
            f"{path} is not a {LEGACY_SUFFIX} artifact; the safe reader handles every other graph"
        )
    if len(reason.strip()) < 8:
        raise TrustedLegacyGraphPickleError(
            "a trust reason of at least 8 characters is required and is recorded in provenance"
        )
    return LegacyGraphDeclaration(source=path, reason=reason.strip())


class _RestrictedUnpickler(pickle.Unpickler):
    """Unpickler that resolves only :data:`ALLOWED_GLOBALS`."""

    def find_class(self, module: str, name: str) -> Any:
        if (module, name) not in ALLOWED_GLOBALS:
            raise TrustedLegacyGraphPickleError(
                f"legacy graph pickle referenced the disallowed global {module}.{name}; "
                "this file is not a plain relation graph and was not loaded"
            )
        return super().find_class(module, name)


def _warn(declaration: LegacyGraphDeclaration, stream: TextIO | None) -> None:
    target = sys.stderr if stream is None else stream
    print(
        "warning: loading executable serialization (pickle) from "
        f"{declaration.source}\n"
        f"warning: trusted only because: {declaration.reason}\n"
        f"warning: recording {PROVENANCE_KEY}=true; no pipeline stage reads this format",
        file=target,
    )


def load_trusted_legacy_graph(
    declaration: LegacyGraphDeclaration,
    *,
    allow_trusted_legacy_graph_pickle: bool = False,
    stream: TextIO | None = None,
) -> nx.Graph:
    """Deserialize a trusted legacy relation graph. Both gates must be open."""
    if not allow_trusted_legacy_graph_pickle:
        raise TrustedLegacyGraphPickleError(
            f"refusing to deserialize {declaration.source}: legacy graph pickle reading is "
            f"disabled by default; pass {OPT_IN_FLAG} only for a file you produced yourself"
        )
    if not declaration.source.is_file():
        raise TrustedLegacyGraphPickleError(f"legacy graph pickle not found: {declaration.source}")

    _warn(declaration, stream)
    payload = declaration.source.read_bytes()
    try:
        graph = _RestrictedUnpickler(io.BytesIO(payload)).load()
    except TrustedLegacyGraphPickleError:
        raise
    except Exception as error:  # noqa: BLE001 - any decode failure must surface as a refusal
        raise TrustedLegacyGraphPickleError(
            f"{declaration.source} could not be read as a legacy relation graph: {error}"
        ) from error

    if not isinstance(graph, nx.Graph):
        raise TrustedLegacyGraphPickleError(
            f"{declaration.source} deserialized to {type(graph).__name__}, not a networkx.Graph"
        )
    if graph.is_directed() or graph.is_multigraph():
        raise TrustedLegacyGraphPickleError(
            f"{declaration.source} is not an undirected simple graph"
        )
    return graph


def provenance_record(
    declaration: LegacyGraphDeclaration, meta: GraphArtifactMeta, *, destination: Path
) -> dict[str, Any]:
    """Audit record for a conversion, written beside the converted artifact."""
    payload = declaration.source.read_bytes()
    return {
        "schema": PROVENANCE_SCHEMA,
        PROVENANCE_KEY: True,
        "source": {
            "name": declaration.source.name,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "format": "python_pickle",
        },
        "trust_reason": declaration.reason,
        "converted_to": {
            "name": destination.name,
            "format": meta.format,
            "schema_version": meta.schema_version,
            "node_count": meta.node_count,
            "edge_count": meta.edge_count,
            "semantic_digest": meta.semantic_digest,
        },
    }


def convert_trusted_legacy_graph(
    declaration: LegacyGraphDeclaration,
    destination: str | Path,
    *,
    allow_trusted_legacy_graph_pickle: bool = False,
    stream: TextIO | None = None,
) -> tuple[GraphArtifactMeta, dict[str, Any]]:
    """Convert a trusted legacy pickle into a ``SafeGraphArtifactV1`` artifact.

    Returns the artifact metadata and the provenance record. The graph is
    written unchanged: same nodes, same edges, same attribute values.
    """
    target = Path(destination)
    if target.exists():
        raise TrustedLegacyGraphPickleError(f"refusing to overwrite {target}")

    graph = load_trusted_legacy_graph(
        declaration,
        allow_trusted_legacy_graph_pickle=allow_trusted_legacy_graph_pickle,
        stream=stream,
    )
    expected = graph_meta(graph)
    target.parent.mkdir(parents=True, exist_ok=True)
    meta = write_safe_graph(graph, target)
    if meta.semantic_digest != expected.semantic_digest:
        raise TrustedLegacyGraphPickleError(
            f"conversion of {declaration.source} did not round-trip; {target} was not trusted"
        )
    return meta, provenance_record(declaration, meta, destination=target)
