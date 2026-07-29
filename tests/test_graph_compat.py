from __future__ import annotations

from lib import graph as legacy
from roadnet_partition.graphs import relations as current


PUBLIC_NAMES = {
    "canonical_pair", "build_incident_index", "iter_incident_pairs",
    "empty_edge_record", "ensure_edge_record", "serialize_edge_records",
}


def test_legacy_graph_exports_are_compatibility_aliases() -> None:
    assert set(legacy.__all__) == PUBLIC_NAMES
    for name in PUBLIC_NAMES:
        assert getattr(legacy, name) is getattr(current, name)


def test_graph_helpers_preserve_pair_order_and_serialization() -> None:
    records = [
        {"u": 2, "v": 1, "seg_id": "seg_b"},
        {"u": 1, "v": 3, "seg_id": "seg_a"},
        {"u": 1, "v": 4, "seg_id": "seg_c"},
    ]
    incident = current.build_incident_index(records)
    assert incident[1] == {"seg_a", "seg_b", "seg_c"}
    assert list(current.iter_incident_pairs(incident[1])) == [
        ("seg_a", "seg_b"), ("seg_a", "seg_c"), ("seg_b", "seg_c")
    ]
    assert current.canonical_pair("b", "a") == ("a", "b")
    assert current.canonical_pair("a", "a") is None

    edge_map = {}
    edge = current.ensure_edge_record(edge_map, "seg_b", "seg_a")
    edge["relation_types"].update({"direct", "continuity"})
    edge["connector_ids"].update({2, 1})
    edge["connector_highways"].update({"trunk_link", "primary_link"})
    serialized = current.serialize_edge_records(edge_map)
    assert serialized[0]["seg_id_a"] == "seg_a"
    assert serialized[0]["relation_types"] == "continuity|direct"
    assert serialized[0]["connector_ids"] == "1|2"
    assert serialized[0]["connector_highways"] == "primary_link|trunk_link"
