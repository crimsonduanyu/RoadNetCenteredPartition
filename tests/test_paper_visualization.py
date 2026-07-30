from __future__ import annotations

import pandas as pd
import geopandas as gpd
import networkx as nx
from shapely.geometry import LineString

from roadnet_partition.downstream.tte import trip_time_minutes
from roadnet_partition.reporting.best_partition_map import cluster_colors
from roadnet_partition.zoning.metrics import cluster_mean_origin_orders_per_slot


def test_trip_time_minutes_is_shared_by_reporting_and_tte() -> None:
    frame = pd.DataFrame({
        "departure_time": ["2017-06-01 00:00:00", "bad"],
        "finish_time": ["2017-06-01 00:12:30", "2017-06-01 00:00:00"],
    })
    values = trip_time_minutes(frame)
    assert values.iloc[0] == 12.5
    assert pd.isna(values.iloc[1])


def test_adjacent_clusters_receive_distinct_colors() -> None:
    clusters = gpd.GeoDataFrame({
        "seg_id": ["a", "b"],
        "cluster_id": [1, 2],
        "geometry": [LineString([(0, 0), (1, 0)]), LineString([(1, 0), (2, 0)])],
    })
    colors = cluster_colors(clusters, nx.Graph([("a", "b")]), ["#000000", "#ffffff"])
    assert colors[1] != colors[2]


def test_mean_origin_orders_includes_zero_slots() -> None:
    hourly = pd.DataFrame({
        "slot_start": ["2020-01-01 00:00", "2020-01-01 01:00", "2020-01-01 01:00"],
        "origin_seg_id": ["a", "a", "b"],
        "order_count": [4, 2, 6],
    })
    means = cluster_mean_origin_orders_per_slot(hourly, {"a": 1, "b": 2})
    assert means.to_dict() == {1: 3.0, 2: 3.0}
