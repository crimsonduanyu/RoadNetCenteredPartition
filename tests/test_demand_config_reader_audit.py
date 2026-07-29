from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_demand_config_map_preserves_legacy_and_authoritative_readers() -> None:
    entries = json.loads((PROJECT_ROOT / "docs/refactor/config-key-map-v1.json").read_text())["entries"]
    assert len(entries) == 341
    readers = {
        (reader["file"], reader["function"])
        for entry in entries
        for reader in entry.get("readers", [])
    }
    assert ("src/lib/order_dataset.py", "stage_order_assignments") in readers
    assert ("src/roadnet_partition/downstream/demand.py", "stage_order_assignments") in readers
    assert ("src/lib/order_dataset.py", "build_cluster_poi_graph") in readers
    assert ("src/roadnet_partition/graphs/build.py", "build_cluster_poi_graph") in readers
