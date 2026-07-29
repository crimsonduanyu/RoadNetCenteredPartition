from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_supply_config_map_preserves_stage3_and_new_runner_readers() -> None:
    data = json.loads((PROJECT_ROOT / "docs/refactor/config-key-map-v1.json").read_text())
    assert data["mapping_key_count"] == 341
    assert len(data["entries"]) == 341
    readers_by_key = {
        entry["key_path"]: {(reader["file"], reader["function"]) for reader in entry.get("readers", [])}
        for entry in data["entries"]
    }
    for key in [
        "stage3_supply", "stage3_supply.orders_path", "stage3_supply.max_gap_minutes",
        "stage3_supply.tau_idle_minutes", "stage3_supply.carpool_merge_gap_s",
        "stage3_supply.slot_duration_min", "stage3_supply.n_blocks",
    ]:
        assert ("src/roadnet_partition/downstream/supply.py", "run_supply") in readers_by_key[key]
    assert ("src/stages/stage3_supply.py", "build_parser") in readers_by_key["stage3_supply.orders_path"]
