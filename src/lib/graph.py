"""Compatibility bridge for :mod:`roadnet_partition.graphs.relations`."""

from roadnet_partition.graphs.relations import (
    build_incident_index,
    canonical_pair,
    empty_edge_record,
    ensure_edge_record,
    iter_incident_pairs,
    serialize_edge_records,
)

__all__ = [
    "canonical_pair",
    "build_incident_index",
    "iter_incident_pairs",
    "empty_edge_record",
    "ensure_edge_record",
    "serialize_edge_records",
]
