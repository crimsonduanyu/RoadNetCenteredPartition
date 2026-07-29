from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_zoning_config_map_preserves_legacy_and_new_reader_evidence() -> None:
    entries = json.loads((PROJECT_ROOT / "docs/refactor/config-key-map-v1.json").read_text())["entries"]
    readers = {
        (reader["file"], reader["function"])
        for entry in entries
        for reader in entry.get("readers", [])
    }
    assert ("src/lib/regularized.py", "run_from_config") in readers
    assert ("src/roadnet_partition/zoning/partition.py", "run_from_config") in readers
    assert ("src/stages/stage1_partition.py", "build_regularized_config") in readers
    assert ("src/roadnet_partition/zoning/partition.py", "build_regularized_config") in readers
    assert ("src/adaptive_clustering.py", "adaptive_params") in readers
    assert ("src/roadnet_partition/zoning/algorithms/adaptive.py", "adaptive_params") in readers
