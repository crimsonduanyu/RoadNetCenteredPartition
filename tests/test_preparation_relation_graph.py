from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from roadnet_partition.pipeline import preparation


def _config() -> dict:
    return {
        "graph_weights": {
            "direct_adjacency_weight": 1.0,
            "connector_adjacency_weight": 0.5,
            "continuity_weight": 2.0,
        },
        "similarity": {
            "poi_composition_weight": 0.3,
            "poi_density_weight": 0.2,
            "poi_entropy_weight": 0.1,
            "poi_weight": 1.0,
            "order_weight": 1.0,
        },
        "continuity": {
            "same_name_bonus": 1.0,
            "same_osm_id_bonus": 1.0,
            "same_highway_bonus": 0.5,
            "angle_threshold_deg": 30.0,
        },
    }


def _ordinary() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "seg_id": ["s1", "s2", "s3"],
            "u": [1, 2, 3],
            "v": [2, 3, 4],
            "highway": ["residential", "residential", "tertiary"],
            "name": ["Main", "Main", None],
            "osmid": [100, 100, 200],
            "length": [10.0, 12.0, 8.0],
            "segment_role": ["ordinary", "ordinary", "ordinary"],
        },
        geometry=[
            LineString([(0, 0), (1, 0)]),
            LineString([(1, 0), (2, 0)]),
            LineString([(2, 0), (3, 1)]),
        ],
        crs="EPSG:32650",
    )


def _connectors() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"seg_id": ["c1"], "u": [1], "v": [4], "highway": ["motorway_link"]},
        geometry=[LineString([(0, 0), (3, 1)])],
        crs="EPSG:32650",
    )


def _poi_features() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "seg_id": ["s1", "s2", "s3"],
            "poi_cat_00": [2, 0, 1],
            "poi_cat_01": [0, 1, 0],
            "poi_total": [2, 1, 1],
            "poi_density": [200.0, 83.3, 125.0],
            "poi_entropy": [0.0, 0.0, 0.0],
            "dominant_poi_type": ["food", "office", "food"],
        }
    )


def _order_features() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "seg_id": ["s1", "s2", "s3"],
            "pickup_count": [5, 3, 1],
            "dropoff_count": [1, 4, 2],
            "order_total": [6, 7, 3],
            "pickup_dropoff_imbalance": [4, -1, -1],
            "morning_peak_pickups": [3, 1, 0],
            "evening_peak_pickups": [2, 2, 1],
            "night_pickups": [0, 0, 0],
            "weekday_pickups": [5, 3, 1],
            "weekend_pickups": [0, 0, 0],
            "weekday_weekend_diff": [5, 3, 1],
        }
    )


def test_relation_graph_in_memory_matches_csv_read_path(tmp_path: Path) -> None:
    """PREP-02: _build_relation_graph must produce identical relation_edges whether
    it consumes the in-memory POI/order feature frames or re-reads them from CSV."""
    paths = preparation.output_paths(tmp_path)
    paths["poi_features"].parent.mkdir(parents=True, exist_ok=True)
    poi_df, order_df = _poi_features(), _order_features()
    # Write the CSVs so the CSV-read fallback path can read them back.
    poi_df.to_csv(paths["poi_features"], index=False)
    order_df.to_csv(paths["order_features"], index=False)
    config = _config()
    ordinary, connectors = _ordinary(), _connectors()

    # In-memory path.
    preparation._build_relation_graph(config, paths, ordinary, connectors, poi_df, order_df)
    in_memory_edges = paths["relation_edges"].read_text(encoding="utf-8")

    # CSV-read path (no frames passed -> falls back to pd.read_csv).
    preparation._build_relation_graph(config, paths, ordinary, connectors)
    csv_read_edges = paths["relation_edges"].read_text(encoding="utf-8")

    assert in_memory_edges == csv_read_edges, (
        "PREP-02 equivalence failed: in-memory and CSV-read relation_edges differ"
    )
