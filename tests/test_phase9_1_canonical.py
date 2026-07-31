from __future__ import annotations

from pathlib import Path
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_linux_canonical_decision_is_approved_and_run_bound() -> None:
    value = yaml.safe_load((ROOT / "configs/policies/fifth_ring_linux_canonical_v1.yaml").read_text())
    assert value["status"] == "approved"
    assert value["decision"] == "adopt_linux_as_canonical"
    assert value["scope"] == "fifth_ring"
    assert value["source_run"]["run_id"] == "20260730T020500Z-fifth-ring-full-02fce2f7"
    assert value["previous_canonical"]["archive_id"] == "fifth-ring-windows-v1"
    assert value["consequences"]["deterministic_assignment_v2"] == "deferred"


def test_new_formal_configs_do_not_read_removed_processed_inputs() -> None:
    configs = (
        ROOT / "configs/datasets/fifth_ring.yaml",
        ROOT / "configs/zoning/regularized.yaml",
        ROOT / "configs/pipelines/demand.yaml",
    )
    removed = (
        "data/processed/fifth_ring/segment_nodes.gpkg",
        "data/processed/fifth_ring/segment_relation_edges_road_poi_order.csv",
        "data/processed/fifth_ring/segment_poi_features.csv",
        "data/processed/fifth_ring/segment_order_features.csv",
        "data/processed/fifth_ring/segment_order_od_hourly.csv",
    )
    text = "\n".join(path.read_text() for path in configs)
    assert not any(value in text for value in removed)
    assert "frozen_inputs" not in text
    assert "artifacts/" not in text
