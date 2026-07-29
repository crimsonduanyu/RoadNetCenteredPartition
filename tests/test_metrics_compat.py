from __future__ import annotations

import pickle
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import LineString, Point

from lib import metrics as legacy
from roadnet_partition.zoning import metrics as current


PUBLIC_NAMES = {
    "EPS", "MetricThresholds", "safe_divide", "coefficient_of_variation", "bool_series",
    "nonempty_name", "weighted_quantities", "merge_optional_features", "build_cluster_table",
    "partition_from_clusters", "edge_cut_frame", "weighted_edge_cut_metrics",
    "unweighted_edge_cut_ratio", "connectivity_metrics", "road_integrity_metrics",
    "rectangle_elongation", "shape_metrics", "network_diameter_metrics", "wss_metrics",
    "poi_cluster_metrics", "order_cluster_metrics", "od_metrics", "historical_average_metrics",
    "connector_type_metrics", "compute_benchmark_metrics", "match_points_to_segments",
    "load_or_build_hourly_segment_od",
}


def fixed_fixture():
    clusters = gpd.GeoDataFrame(
        {
            "seg_id": ["a", "b", "c", "d"],
            "cluster_id": [0, 0, 1, 1],
            "length": [10.0, 20.0, 10.0, 20.0],
            "name": ["road-0", "road-0", "road-1", "road-1"],
        },
        geometry=[
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (30, 0)]),
            LineString([(30, 0), (40, 0)]),
            LineString([(40, 0), (60, 0)]),
        ],
        crs="EPSG:32650",
    )
    relation_edges = pd.DataFrame(
        {
            "seg_id_a": ["a", "b", "c"],
            "seg_id_b": ["b", "c", "d"],
            "weight": [1.0, 2.0, 1.0],
            "has_direct": [True, True, True],
            "direct_weight": [1.0, 2.0, 1.0],
            "has_continuity": [True, True, True],
            "continuity_weight": [1.0, 2.0, 1.0],
            "has_connector": [False, True, False],
            "connector_weight": [0.0, 2.0, 0.0],
            "connector_highways": ["", "primary_link", ""],
            "same_name": [True, False, True],
        }
    )
    graph = nx.Graph()
    graph.add_edges_from([("a", "b"), ("b", "c"), ("c", "d")])
    poi = pd.DataFrame({"seg_id": ["a", "b", "c", "d"], "poi_cat_food": [1, 3, 2, 2], "poi_total": [1, 3, 2, 2]})
    orders = pd.DataFrame(
        {
            "seg_id": ["a", "b", "c", "d"],
            "pickup_count": [5, 7, 11, 13],
            "dropoff_count": [4, 8, 10, 14],
            "order_total": [9, 15, 21, 27],
            "pickup_dropoff_imbalance": [1, -1, 1, -1],
        }
    )
    hourly = pd.DataFrame(
        {
            "slot_start": pd.to_datetime(["2020-01-01 08:00", "2020-01-02 08:00"]),
            "origin_seg_id": ["a", "a"],
            "destination_seg_id": ["c", "c"],
            "order_count": [10, 12],
        }
    )
    return clusters, relation_edges, graph, poi, orders, hourly


def test_legacy_metrics_exports_are_compatibility_aliases() -> None:
    assert set(legacy.__all__) == PUBLIC_NAMES
    for name in PUBLIC_NAMES:
        assert getattr(legacy, name) is getattr(current, name)


def test_historical_metric_class_module_path_remains_loadable() -> None:
    historical_global = pickle.loads(b"clib.metrics\nMetricThresholds\n.")
    assert historical_global is current.MetricThresholds
    assert current.MetricThresholds.__module__ == "roadnet_partition.zoning.metrics"


def test_public_metric_helpers_on_fixed_values() -> None:
    assert current.safe_divide(3, 2) == 1.5
    assert np.isnan(current.safe_divide(1, 0))
    assert current.coefficient_of_variation(pd.Series([1.0, 3.0])) == 0.5
    assert current.bool_series(pd.Series([True, "yes", "0", None])).tolist() == [True, True, False, False]
    assert current.nonempty_name(pd.Series(["road", " ", None])).tolist() == [True, False, False]
    assert current.weighted_quantities(pd.Series([1.0, 2.0, 3.0])) == {
        "mean": 2.0, "min": 1.0, "max": 3.0, "median": 2.0
    }
    rectangle = LineString([(0, 0), (2, 0)]).buffer(1).minimum_rotated_rectangle
    assert current.rectangle_elongation(rectangle) >= 1.0


def test_full_benchmark_fixture_preserves_schema_order_and_values() -> None:
    clusters, relation_edges, graph, poi, orders, hourly = fixed_fixture()
    thresholds = current.MetricThresholds(geometry_buffer_m=1.0)
    row, connector = current.compute_benchmark_metrics(
        "road_poi_order", "fixture", "fixed", clusters, relation_edges, graph,
        poi_features=poi, order_features=orders, hourly_od=hourly, thresholds=thresholds,
    )
    legacy_row, legacy_connector = legacy.compute_benchmark_metrics(
        "road_poi_order", "fixture", "fixed", clusters, relation_edges, graph,
        poi_features=poi, order_features=orders, hourly_od=hourly, thresholds=thresholds,
    )
    assert list(row) == list(legacy_row)
    for key in row:
        if isinstance(row[key], float):
            assert np.isclose(row[key], legacy_row[key], equal_nan=True)
        else:
            assert row[key] == legacy_row[key]
    pd.testing.assert_frame_equal(connector, legacy_connector)

    assert row["num_clusters"] == 2
    assert row["connected_cluster_ratio"] == 1.0
    assert row["continuity_edge_cut_ratio"] == 0.5
    assert row["connector_edge_cut_ratio"] == 1.0
    assert list(connector.columns) == [
        "graph_variant", "algorithm", "connector_type", "total_edges",
        "cut_edges", "cut_ratio", "cut_weight_ratio",
    ]


def test_remaining_public_io_helpers_use_fixed_temporary_fixtures(tmp_path: Path) -> None:
    segments = gpd.GeoDataFrame(
        {"seg_id": ["a"]},
        geometry=[LineString([(0, 0), (10, 0)])],
        crs="EPSG:32650",
    )
    points = pd.DataFrame({"lon": [0.0], "lat": [0.0]})
    matched = current.match_points_to_segments(points, "lon", "lat", segments, "EPSG:32650", 1.0)
    assert matched.tolist() == ["a"]

    cache = tmp_path / "segment_order_od_hourly.csv"
    expected = pd.DataFrame(
        {"slot_start": ["2020-01-01 00:00:00"], "origin_seg_id": ["a"], "destination_seg_id": ["b"], "order_count": [2]}
    )
    expected.to_csv(cache, index=False)
    loaded = current.load_or_build_hourly_segment_od(
        {}, {"data_processed": tmp_path, "order_od_hourly": cache}
    )
    assert pd.api.types.is_datetime64_any_dtype(loaded["slot_start"])
    assert loaded["order_count"].tolist() == [2]
