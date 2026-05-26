from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import sqlite3
import gzip

import numpy as np
import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "src" / "06_build_order_region_dataset.py"
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def load_module():
    spec = importlib.util.spec_from_file_location("order_region_pipeline", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_service_type_overlap_components() -> None:
    module = load_module()
    records = [
        (1, "boundary", 0, 10),
        (2, "boundary", 10, 20),
        (3, "chain", 0, 10),
        (4, "chain", 9, 20),
        (5, "chain", 19, 30),
        (6, "direct", 0, 10),
        (7, "direct", 5, 8),
        (8, "solo", 0, 10),
    ]

    labels = module.infer_service_labels(records)

    assert labels[1] == module.EXCLUSIVE
    assert labels[2] == module.EXCLUSIVE
    assert labels[3] == module.CARPOOL
    assert labels[4] == module.CARPOOL
    assert labels[5] == module.CARPOOL
    assert labels[6] == module.CARPOOL
    assert labels[7] == module.CARPOOL
    assert labels[8] == module.EXCLUSIVE


def test_aggregate_od_frame_uses_configured_slot_minutes() -> None:
    module = load_module()
    orders = pd.DataFrame(
        [
            {
                "departure_time_ns": pd.Timestamp("2017-10-16 08:00:00").value,
                "origin_cluster_id": "1",
                "destination_cluster_id": "1",
                "service_type": module.EXCLUSIVE,
            },
            {
                "departure_time_ns": pd.Timestamp("2017-10-16 08:07:00").value,
                "origin_cluster_id": "1",
                "destination_cluster_id": "2",
                "service_type": module.EXCLUSIVE,
            },
            {
                "departure_time_ns": pd.Timestamp("2017-10-16 08:14:59").value,
                "origin_cluster_id": "1",
                "destination_cluster_id": "2",
                "service_type": module.CARPOOL,
            },
            {
                "departure_time_ns": pd.Timestamp("2017-10-16 08:15:00").value,
                "origin_cluster_id": "2",
                "destination_cluster_id": "1",
                "service_type": module.EXCLUSIVE,
            },
            {
                "departure_time_ns": pd.Timestamp("2017-10-16 08:44:59").value,
                "origin_cluster_id": "2",
                "destination_cluster_id": "2",
                "service_type": module.CARPOOL,
            },
            {
                "departure_time_ns": pd.Timestamp("2017-10-16 08:45:00").value,
                "origin_cluster_id": "2",
                "destination_cluster_id": "2",
                "service_type": module.EXCLUSIVE,
            },
        ]
    )

    od = module.aggregate_od_frame(orders, 15)

    exact = od.loc[(od["slot_start"] == "2017-10-16 08:00:00") & (od["origin_cluster_id"] == "1") & (od["destination_cluster_id"] == "1")].iloc[0]
    first = od.loc[(od["slot_start"] == "2017-10-16 08:00:00") & (od["origin_cluster_id"] == "1") & (od["destination_cluster_id"] == "2")].iloc[0]
    second = od.loc[(od["slot_start"] == "2017-10-16 08:15:00") & (od["origin_cluster_id"] == "2") & (od["destination_cluster_id"] == "1")].iloc[0]
    third = od.loc[(od["slot_start"] == "2017-10-16 08:30:00") & (od["origin_cluster_id"] == "2") & (od["destination_cluster_id"] == "2")].iloc[0]
    fourth = od.loc[(od["slot_start"] == "2017-10-16 08:45:00") & (od["origin_cluster_id"] == "2") & (od["destination_cluster_id"] == "2")].iloc[0]
    assert int(exact["exclusive_count"]) == 1
    assert int(first["exclusive_count"]) == 1
    assert int(first["carpool_count"]) == 1
    assert int(first["total_count"]) == 2
    assert int(second["exclusive_count"]) == 1
    assert int(second["carpool_count"]) == 0
    assert int(second["total_count"]) == 1
    assert int(third["carpool_count"]) == 1
    assert int(fourth["exclusive_count"]) == 1


def test_build_cluster_road_edges_aggregates_segment_edges() -> None:
    module = load_module()
    relation_edges = pd.DataFrame(
        [
            {"seg_id_a": "s1", "seg_id_b": "s2", "base_weight": 1.5},
            {"seg_id_a": "s1", "seg_id_b": "s3", "base_weight": 2.5},
            {"seg_id_a": "s2", "seg_id_b": "s3", "base_weight": 10.0},
            {"seg_id_a": "s3", "seg_id_b": "s4", "base_weight": 3.0},
        ]
    )
    segment_to_cluster = {"s1": "1", "s2": "2", "s3": "2", "s4": "3"}
    cluster_to_index = {"1": 0, "2": 1, "3": 2}

    edges = module.build_cluster_road_edges(relation_edges, segment_to_cluster, cluster_to_index)

    pair_weights = {
        (row.cluster_id_a, row.cluster_id_b): row.weight
        for row in edges.itertuples(index=False)
    }
    assert pair_weights == {("1", "2"): 4.0, ("2", "3"): 3.0}
    assert set(edges["num_segment_edges"]) == {1, 2}


def test_build_od_tensors_shape_and_values() -> None:
    module = load_module()
    od = pd.DataFrame(
        [
            {
                "slot_start": "2017-10-16 08:00:00",
                "origin_cluster_id": "1",
                "destination_cluster_id": "2",
                "exclusive_count": 3,
                "carpool_count": 1,
                "total_count": 4,
            }
        ]
    )

    tensors = module.build_od_tensors(od, ["1", "2"])

    assert tensors["Y_total"].shape == (1, 2, 2)
    assert int(tensors["Y_exclusive"][0, 0, 1]) == 3
    assert int(tensors["Y_carpool"][0, 0, 1]) == 1
    assert int(tensors["Y_total"][0, 0, 1]) == 4


def test_build_od_tensors_accepts_continuous_slot_labels() -> None:
    module = load_module()
    od = pd.DataFrame(
        [
            {
                "slot_start": "2017-10-16 08:15:00",
                "origin_cluster_id": "1",
                "destination_cluster_id": "1",
                "exclusive_count": 2,
                "carpool_count": 0,
                "total_count": 2,
            }
        ]
    )
    slots = module.build_slot_labels(
        pd.Timestamp("2017-10-16 08:00:00"),
        pd.Timestamp("2017-10-16 08:45:00"),
        15,
    )

    tensors = module.build_od_tensors(od, ["1"], slots)

    assert tensors["Y_total"].shape == (3, 1, 1)
    assert int(tensors["Y_total"][0, 0, 0]) == 0
    assert int(tensors["Y_total"][1, 0, 0]) == 2
    assert int(tensors["Y_total"][2, 0, 0]) == 0


def test_epoch_helpers_force_nanoseconds_for_ms_datetimes() -> None:
    module = load_module()
    values = pd.Series(np.array(["2017-10-16T08:14:59"], dtype="datetime64[ms]"))

    epoch_ns = module.to_epoch_ns(values)
    floored = module.floor_datetimes_to_slot(values, 15)

    assert int(epoch_ns.iloc[0]) == pd.Timestamp("2017-10-16 08:14:59").value
    assert int(epoch_ns.iloc[0]) > 10**18
    assert floored.iloc[0].strftime("%Y-%m-%d %H:%M:%S") == "2017-10-16 08:00:00"
    assert module.format_timestamp_ns(int(epoch_ns.iloc[0])) == "2017-10-16 08:14:59"


def test_slot_labels_from_staged_bounds_use_actual_data_range() -> None:
    module = load_module()
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE staged_orders (
            departure_time_ns INTEGER NOT NULL,
            slot_start_ns INTEGER NOT NULL
        )
        """
    )
    connection.executemany(
        "INSERT INTO staged_orders(departure_time_ns, slot_start_ns) VALUES (?, ?)",
        [
            (
                pd.Timestamp("2017-10-16 08:07:00").value,
                pd.Timestamp("2017-10-16 08:00:00").value,
            ),
            (
                pd.Timestamp("2017-10-16 08:31:00").value,
                pd.Timestamp("2017-10-16 08:30:00").value,
            ),
        ],
    )

    bounds = module.load_staged_slot_bounds(connection)
    slots = module.build_slot_labels_from_bounds(bounds["min_slot_start_ns"], bounds["max_slot_start_ns"], 15)

    assert slots.tolist() == [
        "2017-10-16 08:00:00",
        "2017-10-16 08:15:00",
        "2017-10-16 08:30:00",
    ]


def test_full_pipeline_smoke_with_tiny_fixture(tmp_path) -> None:
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import LineString
    import yaml

    module = load_module()
    partition_path = tmp_path / "partition.gpkg"
    relation_path = tmp_path / "relation_edges.csv"
    orders_path = tmp_path / "orders.csv"
    poi_path = tmp_path / "poi.csv"
    output_root = tmp_path / "out"

    partition = gpd.GeoDataFrame(
        {
            "seg_id": ["s1", "s2"],
            "cluster_id": ["1", "2"],
            "length": [100.0, 100.0],
        },
        geometry=[
            LineString([(0.0, 0.0), (0.001, 0.0)]),
            LineString([(0.01, 0.0), (0.011, 0.0)]),
        ],
        crs="EPSG:4326",
    ).to_crs("EPSG:32631")
    partition.to_file(partition_path, driver="GPKG")

    pd.DataFrame([{"seg_id_a": "s1", "seg_id_b": "s2", "base_weight": 2.0}]).to_csv(relation_path, index=False)
    pd.DataFrame(
        [
            {
                "order_id": "o1",
                "driver_id": "d1",
                "starting_lng": 0.0002,
                "starting_lat": 0.0,
                "dest_lng": 0.0102,
                "dest_lat": 0.0,
                "departure_time": "2017-10-16 08:01:00",
                "finish_time": "2017-10-16 08:20:00",
            },
            {
                "order_id": "o2",
                "driver_id": "d1",
                "starting_lng": 0.0003,
                "starting_lat": 0.0,
                "dest_lng": 0.0103,
                "dest_lat": 0.0,
                "departure_time": "2017-10-16 08:05:00",
                "finish_time": "2017-10-16 08:25:00",
            },
            {
                "order_id": "o3",
                "driver_id": "d2",
                "starting_lng": 0.0102,
                "starting_lat": 0.0,
                "dest_lng": 0.0002,
                "dest_lat": 0.0,
                "departure_time": "2017-10-16 08:15:00",
                "finish_time": "2017-10-16 08:30:00",
            },
        ]
    ).to_csv(orders_path, index=False)
    pd.DataFrame(
        [
            {"lon": 0.0002, "lat": 0.0, "cat": "food"},
            {"lon": 0.0102, "lat": 0.0, "cat": "office"},
        ]
    ).to_csv(poi_path, index=False)

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
                "chunksize": 2,
                "max_match_distance_m": 250,
                "start_time": "2017-10-16 00:00:00",
                "end_time": "2017-10-17 00:00:00",
                "order_id_column": "order_id",
                "driver_id_column": "driver_id",
                "pickup_lon_column": "starting_lng",
                "pickup_lat_column": "starting_lat",
                "dropoff_lon_column": "dest_lng",
                "dropoff_lat_column": "dest_lat",
                "departure_time_column": "departure_time",
                "finish_time_column": "finish_time",
            },
            "poi": {
                "lon_column": "lon",
                "lat_column": "lat",
                "category_column": "cat",
                "max_match_distance_m": 250,
                "similarity_top_k": 1,
            },
            "road_graph": {"weight_column": "base_weight"},
            "distance_graph": {"top_k": 1, "decay_distance_m": 1000.0},
            "graph_normalization": {"add_self_loops": True, "symmetric": True},
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    module.main([str(config_path)])

    od = pd.read_csv(output_root / "cluster_od_15min.csv")
    assert int(od["carpool_count"].sum()) == 2
    assert int(od["exclusive_count"].sum()) == 1
    assert od["slot_start"].str.startswith("2017-10-16").all()
    assert not od["slot_start"].str.startswith("1970").any()
    with gzip.open(output_root / "orders_region_assigned.csv.gz", "rt", encoding="utf-8") as handle:
        assigned = pd.read_csv(handle)
    assert assigned["departure_time"].str.startswith("2017-10-16").all()
    assert assigned["slot_start"].str.startswith("2017-10-16").all()
    assert not assigned["departure_time"].str.startswith("1970").any()
    tensor = np.load(output_root / "od_tensor_15min.npz")
    assert tensor["slot_start"].tolist() == [
        "2017-10-16 08:00:00",
        "2017-10-16 08:15:00",
    ]
    for name in [
        "orders_region_assigned.csv.gz",
        "od_tensor_15min.npz",
        "cluster_index.csv",
        "cluster_graph_road_edges.csv",
        "cluster_graph_road_adjacency_raw.npz",
        "cluster_graph_poi_edges.csv",
        "cluster_graph_distance_edges.csv",
        "metadata.json",
    ]:
        assert (output_root / name).exists()
