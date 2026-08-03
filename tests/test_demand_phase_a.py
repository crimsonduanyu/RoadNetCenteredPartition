from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

import pandas as pd
import pytest
import pyarrow.parquet as pq

from roadnet_partition.config import ResolvedStageConfig, config_fingerprint
from roadnet_partition.downstream.demand import run_demand, run_from_config
from roadnet_partition.downstream.order_checkpoints import (
    LABELED_ORDER_CHECKPOINT,
    load_checkpoint_manifest,
)
from roadnet_partition.pipeline.stages import ResumeConflictError, execute_stage
from roadnet_partition.pipeline.results import RunContext

from test_demand_tiny_equivalence import build_fixture


def test_tiny_phase_a_v2_is_exact_and_keeps_driver_boundary_shards(tmp_path: Path, monkeypatch) -> None:
    config, config_path = build_fixture(tmp_path, tmp_path / "unused")
    v1_config = copy.deepcopy(config)
    v2_config = copy.deepcopy(config)
    v2_config.update(
        {
            "order_staging_backend": "parquet_duckdb_v2",
            "order_staging": {
                "memory_limit": "64MB",
                "threads": 1,
                "batch_size": 1,
                "target_shard_rows": 2,
                "temp_disk_budget_bytes": 1024**3,
                "compatibility_export": True,
            },
        }
    )
    monkeypatch.delenv("ROADNET_DEMAND_TIMING", raising=False)
    v1_resolved = ResolvedStageConfig(config_path, v1_config, config_fingerprint(v1_config))
    v2_resolved = ResolvedStageConfig(config_path, v2_config, config_fingerprint(v2_config))
    run_demand(v1_resolved, RunContext("v1", tmp_path / "v1", tmp_path).for_stage("demand"))
    run_demand(v2_resolved, RunContext("v2", tmp_path / "v2", tmp_path).for_stage("demand"))

    v1_root = tmp_path / "v1" / "demand"
    v2_root = tmp_path / "v2" / "demand"
    with gzip.open(v1_root / "orders_region_assigned.csv.gz", "rb") as left, gzip.open(
        v2_root / "orders_region_assigned.csv.gz", "rb"
    ) as right:
        assert left.read() == right.read()
    for name in (
        "cluster_index.csv",
        "cluster_od_15min.csv",
        "od_tensor_15min.npz",
        "cluster_graph_road_edges.csv",
        "cluster_graph_poi_edges.csv",
        "cluster_graph_distance_edges.csv",
    ):
        assert (v1_root / name).read_bytes() == (v2_root / name).read_bytes(), name
    v1_metadata = json.loads((v1_root / "metadata.json").read_text(encoding="utf-8"))
    v2_metadata = json.loads((v2_root / "metadata.json").read_text(encoding="utf-8"))
    for key in ("order_stats", "service_type_counts", "num_clusters", "num_tensor_slots"):
        assert v1_metadata[key] == v2_metadata[key]

    manifest_path = v2_root / "columnar" / "labeled" / "checkpoint_manifest.json"
    manifest = load_checkpoint_manifest(manifest_path, expected_kind=LABELED_ORDER_CHECKPOINT)
    assert manifest["duckdb_version"] == "1.5.5"
    for shard in manifest["shards"]:
        table = pq.read_table(v2_root / "columnar" / "labeled" / shard["path"], columns=["driver_id"])
        assert len(set(table["driver_id"].to_pylist())) == 1
    assert not (v2_root / "orders_region_staging.sqlite").exists()
    assert not list((v2_root / "columnar").glob("duckdb-temp-*"))


def test_phase_a_compatibility_export_false_is_explicit_experiment(tmp_path: Path, monkeypatch) -> None:
    config, config_path = build_fixture(tmp_path, tmp_path / "unused")
    config.update(
        {
            "order_staging_backend": "parquet_duckdb_v2",
            "order_staging": {
                "memory_limit": "64MB",
                "threads": 1,
                "batch_size": 2,
                "target_shard_rows": 2,
                "temp_disk_budget_bytes": 1024**3,
                "compatibility_export": False,
            },
        }
    )
    config["order_pipeline"]["outputs"]["root"] = str(tmp_path / "no-csv")
    monkeypatch.delenv("ROADNET_DEMAND_TIMING", raising=False)
    run_from_config(config)
    assert not (tmp_path / "no-csv" / "orders_region_assigned.csv.gz").exists()
    assert (tmp_path / "no-csv" / "columnar" / "labeled" / "_CHECKPOINT_COMPLETE").exists()


def test_phase_a_zero_matched_orders_still_publishes_empty_checkpoint(tmp_path: Path, monkeypatch) -> None:
    config, config_path = build_fixture(tmp_path, tmp_path / "empty")
    orders_path = Path(config["order_pipeline"]["inputs"]["order_datasets"][0])
    orders = pd.read_csv(orders_path)
    orders[["starting_lng", "starting_lat", "dest_lng", "dest_lat"]] = 100.0
    orders.to_csv(orders_path, index=False)
    config.update(
        {
            "order_staging_backend": "parquet_duckdb_v2",
            "order_staging": {
                "memory_limit": "64MB",
                "threads": 1,
                "batch_size": 2,
                "target_shard_rows": 2,
                "temp_disk_budget_bytes": 1024**3,
                "compatibility_export": True,
            },
        }
    )
    monkeypatch.delenv("ROADNET_DEMAND_TIMING", raising=False)
    run_from_config(config)
    root = tmp_path / "empty"
    matched = load_checkpoint_manifest(root / "columnar" / "matched" / "checkpoint_manifest.json")
    labeled = load_checkpoint_manifest(root / "columnar" / "labeled" / "checkpoint_manifest.json")
    assert matched["row_count"] == labeled["row_count"] == 0
    with gzip.open(root / "orders_region_assigned.csv.gz", "rb") as handle:
        assert handle.read() == b""


def test_phase_a_stage_resume_checks_checkpoint_runtime_and_overwrite(tmp_path: Path, monkeypatch) -> None:
    config, config_path = build_fixture(tmp_path, tmp_path / "unused")
    config.update(
        {
            "order_staging_backend": "parquet_duckdb_v2",
            "order_staging": {
                "memory_limit": "64MB",
                "threads": 1,
                "batch_size": 2,
                "target_shard_rows": 2,
                "temp_disk_budget_bytes": 1024**3,
                "compatibility_export": True,
            },
        }
    )
    resolved = ResolvedStageConfig(
        config_path,
        config,
        config_fingerprint(config),
        stage="demand",
        scope="tiny",
        project_root=tmp_path,
    )
    run_dir = tmp_path / "stage-run"
    monkeypatch.delenv("ROADNET_DEMAND_TIMING", raising=False)
    execute_stage(stage="demand", config=resolved, run_dir=run_dir, run_id="tiny-v2")
    assert execute_stage(stage="demand", config=resolved, run_dir=run_dir, resume=True).metrics["resume_reused"] is True

    manifest_path = run_dir / "demand" / "columnar" / "labeled" / "checkpoint_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["duckdb_version"] = "0.0.0-tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ResumeConflictError, match="DuckDB|duckdb|checkpoint"):
        execute_stage(stage="demand", config=resolved, run_dir=run_dir, resume=True)

    execute_stage(stage="demand", config=resolved, run_dir=run_dir, overwrite=True)
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["duckdb_version"] == "1.5.5"


def test_phase_a_failed_stage_cleans_owned_temp_and_partial_files(tmp_path: Path, monkeypatch) -> None:
    config, config_path = build_fixture(tmp_path, tmp_path / "unused")
    config.update(
        {
            "order_staging_backend": "parquet_duckdb_v2",
            "order_staging": {
                "memory_limit": "64MB",
                "threads": 1,
                "batch_size": 2,
                "target_shard_rows": 2,
                "temp_disk_budget_bytes": 1024**3,
                "compatibility_export": True,
            },
        }
    )
    resolved = ResolvedStageConfig(
        config_path,
        config,
        config_fingerprint(config),
        stage="demand",
        scope="tiny",
        project_root=tmp_path,
    )

    def fail_after_creating_temp(values, **_kwargs):
        root = Path(values["order_pipeline"]["outputs"]["root"])
        (root / "columnar" / "duckdb-temp-sort").mkdir(parents=True)
        (root / "columnar" / "duckdb-temp-sort" / "spill.bin").write_bytes(b"spill")
        (root / "columnar" / "matched").mkdir(parents=True)
        (root / "columnar" / "matched" / ".chunk.parquet.partial").write_bytes(b"partial")
        raise RuntimeError("injected Phase A failure")

    monkeypatch.setattr("roadnet_partition.downstream.demand.run_from_config", fail_after_creating_temp)
    with pytest.raises(RuntimeError, match="injected"):
        execute_stage(stage="demand", config=resolved, run_dir=tmp_path / "failed", run_id="tiny-failed")
    columnar = tmp_path / "failed" / "demand" / "columnar"
    assert not (columnar / "duckdb-temp-sort").exists()
    assert not list(columnar.rglob("*.partial"))
