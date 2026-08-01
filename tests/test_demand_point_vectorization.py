from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import points as vectorized_points
from shapely.geometry import LineString, Point

import roadnet_partition.io.geospatial as geospatial


def _legacy_match(frame, lon_col, lat_col, segments, source_crs, max_distance_m):
    result = pd.DataFrame(index=frame.index, data={"seg_id": pd.NA, "distance_m": np.nan})
    valid = frame[[lon_col, lat_col]].notna().all(axis=1)
    valid &= np.isfinite(frame[lon_col]) & np.isfinite(frame[lat_col])
    if not bool(valid.any()):
        return result
    points = gpd.GeoDataFrame(
        {"row_id": frame.index[valid]},
        geometry=[Point(xy) for xy in zip(frame.loc[valid, lon_col], frame.loc[valid, lat_col])],
        crs=source_crs,
    ).to_crs(segments.crs)
    joined = gpd.sjoin_nearest(
        points,
        segments[["seg_id", "geometry"]],
        how="left",
        max_distance=max_distance_m,
        distance_col="distance_m",
    )
    matched = joined.dropna(subset=["seg_id"]).drop_duplicates("row_id")
    if not matched.empty:
        result.loc[matched["row_id"].to_numpy(), "seg_id"] = matched["seg_id"].astype(str).to_numpy()
        result.loc[matched["row_id"].to_numpy(), "distance_m"] = matched["distance_m"].astype(float).to_numpy()
    return result


def _legacy_points(frame, lon_col, lat_col, source_crs, target_crs):
    valid = frame[[lon_col, lat_col]].notna().all(axis=1)
    valid &= np.isfinite(frame[lon_col]) & np.isfinite(frame[lat_col])
    return gpd.GeoDataFrame(
        {"row_id": frame.index[valid]},
        geometry=[Point(xy) for xy in zip(frame.loc[valid, lon_col], frame.loc[valid, lat_col])],
        crs=source_crs,
    ).to_crs(target_crs)


def _join_signature(joined):
    return joined[["row_id", "seg_id", "distance_m"]].reset_index(drop=True)


def test_demand_pickup_and_dropoff_vectorized_points_are_legacy_equivalent(monkeypatch) -> None:
    source_crs = "EPSG:4326"
    target_crs = "EPSG:3857"
    segments = gpd.GeoDataFrame(
        {"seg_id": ["right", "left", "far"]},
        geometry=[
            LineString([(0.001, -0.01), (0.001, 0.01)]),
            LineString([(-0.001, -0.01), (-0.001, 0.01)]),
            LineString([(0.1, -0.01), (0.1, 0.01)]),
        ],
        crs=source_crs,
    ).to_crs(target_crs)
    frame = pd.DataFrame(
        {
            "pickup_lon": [0.0002, 0.0002, 0.0, -0.0009, np.nan, np.inf, 0.1],
            "pickup_lat": [0.0] * 7,
            "dropoff_lon": [-0.0002, -0.0002, 0.0, 0.0009, np.nan, -np.inf, 0.1],
            "dropoff_lat": [0.0] * 7,
        },
        index=[101, 7, 500, 42, 8, 9, 1000],
    )
    calls = []

    def capture_points(x, y):
        geometry = vectorized_points(x, y)
        calls.append(geometry)
        return geometry

    monkeypatch.setattr(geospatial, "shapely_points", capture_points)
    for tag, lon_col, lat_col in (
        ("pickup", "pickup_lon", "pickup_lat"),
        ("dropoff", "dropoff_lon", "dropoff_lat"),
    ):
        old_points = _legacy_points(frame, lon_col, lat_col, source_crs, target_crs)
        new_match = geospatial.match_points_to_segments_with_distance(
            frame, lon_col, lat_col, segments, source_crs, 250.0, tag=tag,
        )
        new_points = gpd.GeoDataFrame(
            {"row_id": old_points["row_id"]},
            geometry=calls[-1],
            crs=source_crs,
        ).to_crs(target_crs)

        assert len(old_points.geometry) == len(new_points.geometry) == 5
        assert old_points.index.equals(new_points.index)
        assert old_points.crs == new_points.crs
        assert [geometry.wkb for geometry in old_points.geometry] == [geometry.wkb for geometry in new_points.geometry]
        np.testing.assert_array_equal(old_points.geometry.x.to_numpy(), new_points.geometry.x.to_numpy())
        np.testing.assert_array_equal(old_points.geometry.y.to_numpy(), new_points.geometry.y.to_numpy())

        old_join = gpd.sjoin_nearest(
            old_points, segments[["seg_id", "geometry"]], how="left", max_distance=250.0, distance_col="distance_m",
        )
        new_join = gpd.sjoin_nearest(
            new_points, segments[["seg_id", "geometry"]], how="left", max_distance=250.0, distance_col="distance_m",
        )
        pd.testing.assert_frame_equal(_join_signature(old_join), _join_signature(new_join))
        old_match = _legacy_match(frame, lon_col, lat_col, segments, source_crs, 250.0)
        pd.testing.assert_frame_equal(old_match, new_match)
        assert old_match.index.equals(new_match.index)
        assert old_match["seg_id"].isna().equals(new_match["seg_id"].isna())
        assert old_match.loc[500, "seg_id"] == new_match.loc[500, "seg_id"]

    assert len(calls) == 2
