from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

import pyarrow.parquet as pq

from roadnet_partition.config import ResolvedStageConfig, config_fingerprint
from roadnet_partition.downstream.demand import run_demand, run_from_config
from roadnet_partition.downstream.order_checkpoints import (
    LABELED_ORDER_CHECKPOINT,
    load_checkpoint_manifest,
)
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
