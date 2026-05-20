from __future__ import annotations

from collections import Counter

import env_setup  # noqa: F401
import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from utils_geo import DATA_INTERIM, DATA_PROCESSED, PROJECT_ROOT, ensure_directories, load_config, project_gdf


def match_points_to_segments(
    frame: pd.DataFrame,
    lon_col: str,
    lat_col: str,
    segments: gpd.GeoDataFrame,
    source_crs: str,
    max_distance_m: float,
) -> pd.Series:
    matches = pd.Series(pd.NA, index=frame.index, dtype="object")
    valid = frame[[lon_col, lat_col]].notna().all(axis=1)
    valid &= np.isfinite(frame[lon_col]) & np.isfinite(frame[lat_col])
    if not bool(valid.any()):
        return matches

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
        distance_col="match_distance_m",
    )
    matched = joined.dropna(subset=["seg_id"]).drop_duplicates("row_id")
    matches.loc[matched["row_id"].to_numpy()] = matched["seg_id"].to_numpy()
    return matches


def add_counts(target: Counter, values: pd.Series) -> None:
    target.update(value for value in values.dropna().astype(str))


def main() -> None:
    ensure_directories()
    config = load_config()
    order_config = config["semantic_graph"]["order"]

    segments_path = DATA_INTERIM / "road_edges_classified.gpkg"
    features_path = DATA_PROCESSED / "segment_order_features.csv"
    od_path = DATA_PROCESSED / "segment_order_od_pairs.csv"
    order_path = PROJECT_ROOT / order_config["input_path"]

    print(f"Loading classified roads from {segments_path}...")
    segments = gpd.read_file(segments_path)
    segments = segments.loc[segments["segment_role"] == "ordinary"].copy()
    segments = project_gdf(segments, config["crs"]["projected"]).copy()
    segment_ids = segments["seg_id"].tolist()
    segment_id_set = set(segment_ids)

    pickup_counts: Counter[str] = Counter()
    dropoff_counts: Counter[str] = Counter()
    morning_counts: Counter[str] = Counter()
    evening_counts: Counter[str] = Counter()
    night_counts: Counter[str] = Counter()
    weekday_counts: Counter[str] = Counter()
    weekend_counts: Counter[str] = Counter()
    od_counts: Counter[tuple[str, str]] = Counter()

    total_rows = 0
    window_rows = 0
    pickup_matched = 0
    dropoff_matched = 0

    time_col = order_config["time_column"]
    start_time = pd.Timestamp(order_config["start_time"])
    end_time = pd.Timestamp(order_config["end_time"])
    usecols = [
        order_config["pickup_lon_column"],
        order_config["pickup_lat_column"],
        order_config["dropoff_lon_column"],
        order_config["dropoff_lat_column"],
        time_col,
    ]

    print(f"Reading orders from {order_path} in chunks...")
    for chunk_index, chunk in enumerate(
        pd.read_csv(order_path, usecols=usecols, chunksize=int(order_config["chunksize"])),
        start=1,
    ):
        total_rows += len(chunk)
        chunk[time_col] = pd.to_datetime(chunk[time_col], errors="coerce")
        chunk = chunk.loc[(chunk[time_col] >= start_time) & (chunk[time_col] < end_time)].copy()
        if chunk.empty:
            if chunk_index % 20 == 0:
                print(f"processed chunks: {chunk_index:,}, rows read: {total_rows:,}, rows in window: {window_rows:,}")
            continue

        chunk = chunk.reset_index(drop=True)
        window_rows += len(chunk)
        pickup_seg = match_points_to_segments(
            chunk,
            order_config["pickup_lon_column"],
            order_config["pickup_lat_column"],
            segments,
            config["crs"]["geographic"],
            float(order_config["max_match_distance_m"]),
        )
        dropoff_seg = match_points_to_segments(
            chunk,
            order_config["dropoff_lon_column"],
            order_config["dropoff_lat_column"],
            segments,
            config["crs"]["geographic"],
            float(order_config["max_match_distance_m"]),
        )

        pickup_matched += int(pickup_seg.notna().sum())
        dropoff_matched += int(dropoff_seg.notna().sum())
        add_counts(pickup_counts, pickup_seg)
        add_counts(dropoff_counts, dropoff_seg)

        hours = chunk[time_col].dt.hour
        is_weekend = chunk[time_col].dt.dayofweek >= 5
        add_counts(morning_counts, pickup_seg.loc[hours.isin(order_config["morning_peak_hours"])])
        add_counts(evening_counts, pickup_seg.loc[hours.isin(order_config["evening_peak_hours"])])
        add_counts(night_counts, pickup_seg.loc[hours.isin(order_config["night_hours"])])
        add_counts(weekday_counts, pickup_seg.loc[~is_weekend])
        add_counts(weekend_counts, pickup_seg.loc[is_weekend])

        valid_od = pickup_seg.notna() & dropoff_seg.notna()
        for origin, destination in zip(pickup_seg.loc[valid_od].astype(str), dropoff_seg.loc[valid_od].astype(str)):
            if origin in segment_id_set and destination in segment_id_set:
                od_counts[(origin, destination)] += 1

        print(
            "processed chunks: "
            f"{chunk_index:,}, rows read: {total_rows:,}, rows in window: {window_rows:,}, "
            f"pickup matched: {pickup_matched:,}, dropoff matched: {dropoff_matched:,}"
        )

    features = pd.DataFrame({"seg_id": segment_ids})
    features["pickup_count"] = features["seg_id"].map(pickup_counts).fillna(0).astype(int)
    features["dropoff_count"] = features["seg_id"].map(dropoff_counts).fillna(0).astype(int)
    features["order_total"] = features["pickup_count"] + features["dropoff_count"]
    features["pickup_dropoff_imbalance"] = features["pickup_count"] - features["dropoff_count"]
    features["morning_peak_pickups"] = features["seg_id"].map(morning_counts).fillna(0).astype(int)
    features["evening_peak_pickups"] = features["seg_id"].map(evening_counts).fillna(0).astype(int)
    features["night_pickups"] = features["seg_id"].map(night_counts).fillna(0).astype(int)
    features["weekday_pickups"] = features["seg_id"].map(weekday_counts).fillna(0).astype(int)
    features["weekend_pickups"] = features["seg_id"].map(weekend_counts).fillna(0).astype(int)
    features["weekday_weekend_diff"] = features["weekday_pickups"] - features["weekend_pickups"]

    od_rows = [
        {"origin_seg_id": origin, "destination_seg_id": destination, "order_count": count}
        for (origin, destination), count in sorted(od_counts.items())
    ]
    od_frame = pd.DataFrame(od_rows, columns=["origin_seg_id", "destination_seg_id", "order_count"])

    features.to_csv(features_path, index=False)
    od_frame.to_csv(od_path, index=False)

    print(f"order date window: [{start_time}, {end_time})")
    print(f"number of rows read: {total_rows:,}")
    print(f"number of rows inside date window: {window_rows:,}")
    print(f"pickup matched/unmatched: {pickup_matched:,}/{window_rows - pickup_matched:,}")
    print(f"dropoff matched/unmatched: {dropoff_matched:,}/{window_rows - dropoff_matched:,}")
    print(f"number of nonzero segment OD pairs: {len(od_frame):,}")
    print(f"Saved segment order features to {features_path}")
    print(f"Saved segment OD pairs to {od_path}")


if __name__ == "__main__":
    main()
