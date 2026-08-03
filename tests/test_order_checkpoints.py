from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from roadnet_partition.downstream.order_checkpoints import (
    DriverBoundaryCheckpointWriter,
    LABELED_COLUMNS,
    LABELED_ORDER_CHECKPOINT,
    MATCHED_COLUMNS,
    MATCHED_ORDER_CHECKPOINT,
    ParquetCheckpointWriter,
    checkpoint_schema_fingerprint,
    deterministic_shard_id,
    iter_checkpoint_batches,
    load_checkpoint_manifest,
    sorted_checkpoint_batches,
    validate_checkpoint_manifest,
)


def matched_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "stage_id": [3, 1, 2, 4],
            "source_file": ["orders.csv"] * 4,
            "source_row": [3, 1, 2, 4],
            "order_id": ["o3", "o1", "o2", "o4"],
            "driver_id": ["d2", "d1", "d1", "d1"],
            "departure_time_ns": [2, 1, 1, 3],
            "finish_time_ns": [3, 2, 2, 4],
            "slot_start_ns": [0, 0, 0, 0],
            "pickup_seg_id": ["s2", "s1", "s1", "s1"],
            "dropoff_seg_id": ["s1", "s2", "s2", "s2"],
            "origin_cluster_id": ["2", "1", "1", "1"],
            "destination_cluster_id": ["1", "2", "2", "2"],
            "pickup_match_distance_m": [1.0, 1.0, 1.0, 1.0],
            "dropoff_match_distance_m": [2.0, 2.0, 2.0, 2.0],
        }
    ).loc[:, MATCHED_COLUMNS]


def labeled_frame(driver: str, stage_ids: list[int]) -> pd.DataFrame:
    frame = matched_frame().iloc[: len(stage_ids)].copy()
    frame["stage_id"] = stage_ids
    frame["driver_id"] = driver
    frame["service_type"] = ["exclusive"] * len(frame)
    return frame.loc[:, LABELED_COLUMNS]


def test_matched_contract_records_typed_shards_and_atomic_completion(tmp_path: Path) -> None:
    writer = ParquetCheckpointWriter(
        tmp_path / "matched",
        kind=MATCHED_ORDER_CHECKPOINT,
        source_fingerprint="source",
        config_fingerprint="config",
        runtime={"python": "test"},
        duckdb_version=None,
        target_rows=2,
    )
    writer.write_frame(matched_frame().iloc[:2], deterministic_shard_id("matched", 0, 0))
    writer.write_frame(matched_frame().iloc[2:], deterministic_shard_id("matched", 0, 1))
    manifest_path = writer.finish()

    manifest = load_checkpoint_manifest(manifest_path, expected_kind=MATCHED_ORDER_CHECKPOINT)
    assert manifest["row_count"] == 4
    assert manifest["global_ordinal_range"] == [0, 4]
    assert manifest["min_stage_id"] == 1
    assert manifest["max_stage_id"] == 4
    assert (manifest_path.parent / "_CHECKPOINT_COMPLETE").is_file()
    assert not list(manifest_path.parent.rglob("*.partial"))
    assert manifest["arrow_schema_fingerprint"] == checkpoint_schema_fingerprint(MATCHED_ORDER_CHECKPOINT)


def test_label_shards_close_only_at_driver_boundaries(tmp_path: Path) -> None:
    writer = DriverBoundaryCheckpointWriter(
        tmp_path / "labeled",
        kind=LABELED_ORDER_CHECKPOINT,
        source_fingerprint="source",
        config_fingerprint="config",
        runtime={"python": "test"},
        duckdb_version="1.5.5",
        target_rows=2,
    )
    writer.write_driver(labeled_frame("d1", [1, 2]))
    writer.write_driver(labeled_frame("d2", [3]))
    manifest = load_checkpoint_manifest(writer.finish(), expected_kind=LABELED_ORDER_CHECKPOINT)

    assert [shard["row_count"] for shard in manifest["shards"]] == [2, 1]
    for batch in iter_checkpoint_batches(manifest["shards"] and tmp_path / "labeled" / "checkpoint_manifest.json"):
        assert batch.schema.names == list(LABELED_COLUMNS)


def test_duckdb_sort_has_explicit_tie_break_and_arrow_batch_boundary(tmp_path: Path) -> None:
    writer = ParquetCheckpointWriter(
        tmp_path / "matched",
        kind=MATCHED_ORDER_CHECKPOINT,
        source_fingerprint="source",
        config_fingerprint="config",
        runtime={"python": "test"},
        duckdb_version=None,
    )
    writer.write_frame(matched_frame(), "matched-000000")
    manifest_path = writer.finish()
    with sorted_checkpoint_batches(
        manifest_path,
        temp_directory=tmp_path / "run" / "duckdb-temp",
        run_owned_root=tmp_path / "run",
        batch_size=1,
    ) as (batches, metrics):
        ordered = pd.concat([batch.to_pandas() for batch in batches], ignore_index=True)
    assert ordered["stage_id"].tolist() == [1, 2, 4, 3]
    assert metrics["duckdb_version"] == "1.5.5"
    assert metrics["sort_key"] == ["driver_id", "departure_time_ns", "finish_time_ns", "stage_id"]
    assert metrics["sort_wall_seconds"] is not None


def test_partial_file_is_not_visible_and_incomplete_marker_rejects_resume(tmp_path: Path) -> None:
    writer = ParquetCheckpointWriter(
        tmp_path / "matched",
        kind=MATCHED_ORDER_CHECKPOINT,
        source_fingerprint="source",
        config_fingerprint="config",
        runtime={"python": "test"},
        duckdb_version=None,
    )
    writer.write_frame(matched_frame(), "matched-000000")
    manifest_path = writer.finish()
    partial = manifest_path.parent / "shards" / ".interrupted.parquet.partial"
    partial.write_bytes(b"partial")
    assert len(list(iter_checkpoint_batches(manifest_path))) == 1

    complete_marker = manifest_path.parent / "_CHECKPOINT_COMPLETE"
    complete_marker.unlink()
    with pytest.raises(ValueError, match="marker"):
        load_checkpoint_manifest(manifest_path)


def test_wrong_version_hash_and_temp_ownership_are_rejected(tmp_path: Path) -> None:
    writer = ParquetCheckpointWriter(
        tmp_path / "matched",
        kind=MATCHED_ORDER_CHECKPOINT,
        source_fingerprint="source",
        config_fingerprint="config",
        runtime={"python": "test"},
        duckdb_version=None,
    )
    manifest_path = writer.finish()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 99
    with pytest.raises(ValueError, match="contract/version"):
        validate_checkpoint_manifest(manifest)

    with pytest.raises(ValueError, match="not run-owned"):
        with sorted_checkpoint_batches(
            manifest_path,
            temp_directory=tmp_path / "outside",
            run_owned_root=tmp_path / "run",
        ):
            pass
