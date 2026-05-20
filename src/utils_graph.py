from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from typing import Any


def canonical_pair(seg_a: str, seg_b: str) -> tuple[str, str] | None:
    if seg_a == seg_b:
        return None
    return tuple(sorted((seg_a, seg_b)))


def build_incident_index(records: list[dict[str, Any]]) -> dict[Any, set[str]]:
    incident = defaultdict(set)
    for record in records:
        incident[record["u"]].add(record["seg_id"])
        incident[record["v"]].add(record["seg_id"])
    return incident


def iter_incident_pairs(seg_ids: set[str]):
    for seg_a, seg_b in combinations(sorted(seg_ids), 2):
        yield seg_a, seg_b


def empty_edge_record(seg_a: str, seg_b: str) -> dict[str, Any]:
    return {
        "seg_id_a": seg_a,
        "seg_id_b": seg_b,
        "weight": 0.0,
        "base_weight": 0.0,
        "direct_weight": 0.0,
        "connector_weight": 0.0,
        "continuity_weight": 0.0,
        "poi_similarity": 0.0,
        "poi_weight": 0.0,
        "order_similarity": 0.0,
        "order_weight": 0.0,
        "has_direct": False,
        "has_connector": False,
        "has_continuity": False,
        "relation_types": set(),
        "connector_count": 0,
        "connector_ids": set(),
        "connector_highways": set(),
        "continuity_score": 0.0,
        "same_name": False,
        "same_osmid": False,
        "same_highway": False,
        "angle_diff": None,
    }


def ensure_edge_record(edge_map: dict[tuple[str, str], dict[str, Any]], seg_a: str, seg_b: str) -> dict[str, Any]:
    pair = canonical_pair(seg_a, seg_b)
    if pair is None:
        raise ValueError("Self-pairs are not allowed in relation edges.")
    if pair not in edge_map:
        edge_map[pair] = empty_edge_record(*pair)
    return edge_map[pair]


def serialize_edge_records(edge_map: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    serialized = []
    for pair in sorted(edge_map):
        record = edge_map[pair].copy()
        record["relation_types"] = "|".join(sorted(record["relation_types"]))
        record["connector_ids"] = "|".join(sorted(str(x) for x in record["connector_ids"]))
        record["connector_highways"] = "|".join(sorted(str(x) for x in record["connector_highways"]))
        serialized.append(record)
    return serialized
