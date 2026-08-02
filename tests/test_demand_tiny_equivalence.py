from __future__ import annotations

import gzip
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString
import yaml

from roadnet_partition.config import ResolvedStageConfig
from roadnet_partition.downstream.demand import run_demand
from roadnet_partition.downstream.demand_contracts import (
    validate_cluster_index,
    validate_graph_assets,
    validate_od_and_tensor,
)
from roadnet_partition.pipeline.results import RunContext


def build_fixture(tmp_path: Path, output_root: Path) -> tuple[dict, Path]:
    partition_path = tmp_path / "partition.gpkg"
    relation_path = tmp_path / "relation_edges.csv"
    orders_path = tmp_path / "orders.csv"
    poi_path = tmp_path / "poi.csv"
    partition = gpd.GeoDataFrame(
        {"seg_id": ["s1", "s2", "s3"], "cluster_id": ["2", "10", "zone"], "length": [100.0] * 3},
        geometry=[
            LineString([(0.0, 0.0), (0.001, 0.0)]),
            LineString([(0.01, 0.0), (0.011, 0.0)]),
            LineString([(0.02, 0.0), (0.021, 0.0)]),
        ],
        crs="EPSG:4326",
    ).to_crs("EPSG:32631")
    partition.to_file(partition_path, driver="GPKG")
    pd.DataFrame([{"seg_id_a": "s1", "seg_id_b": "s2", "base_weight": 2.0}]).to_csv(relation_path, index=False)
    pd.DataFrame([
        {"order_id": "o1", "driver_id": "d1", "starting_lng": .0002, "starting_lat": 0, "dest_lng": .0102, "dest_lat": 0, "departure_time": "2017-10-16 08:01:00", "finish_time": "2017-10-16 08:20:00"},
        {"order_id": "o2", "driver_id": "d1", "starting_lng": .0003, "starting_lat": 0, "dest_lng": .0103, "dest_lat": 0, "departure_time": "2017-10-16 08:05:00", "finish_time": "2017-10-16 08:25:00"},
        {"order_id": "o3", "driver_id": "d2", "starting_lng": .0102, "starting_lat": 0, "dest_lng": .0202, "dest_lat": 0, "departure_time": "2017-10-16 08:15:00", "finish_time": "2017-10-16 08:30:00"},
        {"order_id": "pickup-unmatched", "driver_id": "d3", "starting_lng": 1.0, "starting_lat": 1.0, "dest_lng": .0102, "dest_lat": 0, "departure_time": "2017-10-16 08:29:59", "finish_time": "2017-10-16 08:40:00"},
        {"order_id": "dropoff-unmatched", "driver_id": "d4", "starting_lng": .0002, "starting_lat": 0, "dest_lng": 1.0, "dest_lat": 1.0, "departure_time": "2017-10-16 08:30:00", "finish_time": "2017-10-16 08:45:00"},
        {"order_id": "end-exclusive", "driver_id": "d5", "starting_lng": .0002, "starting_lat": 0, "dest_lng": .0202, "dest_lat": 0, "departure_time": "2017-10-17 00:00:00", "finish_time": "2017-10-17 00:10:00"},
    ]).to_csv(orders_path, index=False)
    pd.DataFrame([
        {"lon": .0002, "lat": 0, "cat": "food"},
        {"lon": .0102, "lat": 0, "cat": "office"},
    ]).to_csv(poi_path, index=False)
    config = {
        "study_area": {"active": "tiny"},
        "crs": {"projected": "EPSG:32631", "geographic": "EPSG:4326"},
        "order_pipeline": {
            "inputs": {
                "partition_gpkg": str(partition_path),
                "road_relation_edges_csv": str(relation_path),
                "order_datasets": [str(orders_path)],
                "poi_path": str(poi_path),
            },
            "outputs": {"root": str(output_root)},
            "time_slot_minutes": 15,
            "order": {
                "chunksize": 2, "max_match_distance_m": 250,
                "start_time": "2017-10-16 00:00:00", "end_time": "2017-10-17 00:00:00",
                "order_id_column": "order_id", "driver_id_column": "driver_id",
                "pickup_lon_column": "starting_lng", "pickup_lat_column": "starting_lat",
                "dropoff_lon_column": "dest_lng", "dropoff_lat_column": "dest_lat",
                "departure_time_column": "departure_time", "finish_time_column": "finish_time",
            },
            "poi": {"lon_column": "lon", "lat_column": "lat", "category_column": "cat", "max_match_distance_m": 250, "similarity_top_k": 1},
            "road_graph": {"weight_column": "base_weight"},
            "distance_graph": {"top_k": 1, "decay_distance_m": 1000.0},
            "graph_normalization": {"add_self_loops": True, "symmetric": True},
        },
    }
    config_path = tmp_path / "legacy-config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return config, config_path


def test_tiny_demand_outputs_and_contracts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ROADNET_DEMAND_TIMING", raising=False)
    config, config_path = build_fixture(tmp_path, tmp_path / "unused")
    context = RunContext("tiny", tmp_path / "run", tmp_path).for_stage("demand")
    resolved = ResolvedStageConfig(config_path, config, "tiny")
    result = run_demand(resolved, context)
    new_root = context.stage_dir
    assert new_root is not None

    assert "orders_region_staging.sqlite" not in {path.name for path in new_root.iterdir()}
    new_assigned = pd.read_csv(new_root / "orders_region_assigned.csv.gz")
    assert new_assigned["order_id"].tolist() == ["o1", "o2", "o3"]
    assert new_assigned["service_type"].tolist() == ["carpool", "carpool", "exclusive"]

    new_metadata = json.loads((new_root / "metadata.json").read_text())
    assert new_metadata["num_clusters"] == 3
    cluster_ids = validate_cluster_index(pd.read_csv(new_root / "cluster_index.csv", dtype={"cluster_id": str}), ["2", "10", "zone"])
    od_contract = validate_od_and_tensor(new_root / "cluster_od_15min.csv", new_root / "od_tensor_15min.npz", cluster_ids)
    assert od_contract["sums"] == {"exclusive": 1, "carpool": 2, "total": 3}
    assert validate_graph_assets(new_root, "road", 3, add_self_loops=True, symmetric=True)["endpoint_nodes"] == 2
    assert result.metrics["orders"] == 3


def test_tiny_timing_on_off_outputs_are_equal(tmp_path: Path, monkeypatch) -> None:
    config, config_path = build_fixture(tmp_path, tmp_path / "unused")
    config["gzip_compresslevel"] = 1
    resolved = ResolvedStageConfig(config_path, config, "tiny")

    monkeypatch.delenv("ROADNET_DEMAND_TIMING", raising=False)
    run_demand(resolved, RunContext("off", tmp_path / "off", tmp_path).for_stage("demand"))
    monkeypatch.setenv("ROADNET_DEMAND_TIMING", "1")
    run_demand(resolved, RunContext("on", tmp_path / "on", tmp_path).for_stage("demand"))

    off_root = tmp_path / "off" / "demand"
    on_root = tmp_path / "on" / "demand"
    names = sorted(path.name for path in off_root.iterdir() if path.name != "timing_profile.json")
    assert names == sorted(path.name for path in on_root.iterdir() if path.name != "timing_profile.json")
    for name in names:
        off_path = off_root / name
        on_path = on_root / name
        if name == "orders_region_assigned.csv.gz":
            with gzip.open(off_path, "rb") as off_handle, gzip.open(on_path, "rb") as on_handle:
                assert off_handle.read() == on_handle.read()
        elif name == "metadata.json":
            off_metadata = json.loads(off_path.read_text(encoding="utf-8"))
            on_metadata = json.loads(on_path.read_text(encoding="utf-8"))

            def normalize_paths(value):
                if isinstance(value, dict):
                    return {key: normalize_paths(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [normalize_paths(item) for item in value]
                if isinstance(value, str):
                    return value.replace(str(off_root), str(on_root))
                return value

            assert normalize_paths(off_metadata) == on_metadata
        else:
            assert off_path.read_bytes() == on_path.read_bytes(), name

    profile = json.loads((on_root / "timing_profile.json").read_text(encoding="utf-8"))
    phases = {item["phase"] for item in profile["phases"]}
    assert {
        "export_join_execute",
        "export_join_fetch",
        "export_frame_build",
        "export_datetime_format",
        "export_other_format",
        "export_csv_serialize",
        "export_gzip_compress_write",
        "export_flush_close",
    } <= phases
