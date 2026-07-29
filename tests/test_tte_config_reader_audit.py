from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_FILE = "src/lib/tte_dataset.py"
AUTHORITATIVE_FILE = "src/roadnet_partition/downstream/tte.py"


def test_tte_config_map_preserves_legacy_and_authoritative_readers() -> None:
    data = json.loads((PROJECT_ROOT / "docs/refactor/config-key-map-v1.json").read_text())
    assert data["mapping_key_count"] == 341
    assert len(data["entries"]) == 341

    legacy = {
        (entry["key_path"], reader["function"])
        for entry in data["entries"]
        for reader in entry.get("readers", [])
        if reader["file"] == LEGACY_FILE
    }
    authoritative = {
        (entry["key_path"], reader["function"])
        for entry in data["entries"]
        for reader in entry.get("readers", [])
        if reader["file"] == AUTHORITATIVE_FILE
    }
    assert len(legacy) == 45
    assert legacy <= authoritative


def test_tte_config_map_records_stage_entrypoint_readers() -> None:
    entries = json.loads(
        (PROJECT_ROOT / "docs/refactor/config-key-map-v1.json").read_text()
    )["entries"]
    readers_by_key = {
        entry["key_path"]: {
            (reader["file"], reader["function"])
            for reader in entry.get("readers", [])
        }
        for entry in entries
    }
    run_tte = (AUTHORITATIVE_FILE, "run_tte")
    for key in (
        "stage4_tte",
        "stage4_tte.inputs.orders_path",
        "stage4_tte.inputs.cluster_index_path",
        "stage4_tte.outputs.count_filename",
        "stage4_tte.outputs.hops_filename",
        "stage4_tte.outputs.support_filename",
        "stage4_tte.distance.matrix_filename",
        "stage4_tte.distance.representatives_filename",
        "stage4_tte.distance.graphml_path",
        "stage4_tte.distance.classified_edges_path",
        "stage4_tte.distance.partition_gpkg",
        "stage4_tte.time.freq",
        "stage4_tte.time.start_time",
        "stage4_tte.time.end_time",
        "stage4_tte.trip_time.min_minutes",
        "stage4_tte.trip_time.max_minutes",
        "stage4_tte.imputation.max_hops",
    ):
        assert run_tte in readers_by_key[key]

    active_scope = (AUTHORITATIVE_FILE, "active_scope_name")
    assert active_scope in readers_by_key["study_area"]
    assert active_scope in readers_by_key["study_area.active"]
