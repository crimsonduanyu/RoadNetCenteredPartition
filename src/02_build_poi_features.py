from __future__ import annotations

import math

import env_setup  # noqa: F401
import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from utils_geo import DATA_INTERIM, DATA_PROCESSED, PROJECT_ROOT, ensure_directories, load_config, project_gdf


def normalized_entropy(counts: np.ndarray) -> float:
    total = float(counts.sum())
    if total <= 0:
        return 0.0
    probabilities = counts[counts > 0] / total
    if len(probabilities) <= 1:
        return 0.0
    entropy = -float(np.sum(probabilities * np.log(probabilities)))
    return entropy / math.log(len(counts))


def main() -> None:
    ensure_directories()
    config = load_config()
    poi_config = config["semantic_graph"]["poi"]

    segments_path = DATA_INTERIM / "road_edges_classified.gpkg"
    output_path = DATA_PROCESSED / "segment_poi_features.csv"
    category_map_path = DATA_PROCESSED / "poi_category_mapping.csv"

    print(f"Loading classified roads from {segments_path}...")
    segments = gpd.read_file(segments_path)
    segments = segments.loc[segments["segment_role"] == "ordinary"].copy()
    segments = project_gdf(segments, config["crs"]["projected"]).copy()
    segment_ids = segments["seg_id"].tolist()

    poi_path = PROJECT_ROOT / poi_config["input_path"]
    lon_col = poi_config["lon_column"]
    lat_col = poi_config["lat_column"]
    category_col = poi_config["category_column"]
    usecols = [lon_col, lat_col, category_col]

    print(f"Loading POIs from {poi_path}...")
    poi = pd.read_csv(poi_path, usecols=usecols)
    poi = poi.dropna(subset=[lon_col, lat_col, category_col]).copy()
    poi = poi.loc[np.isfinite(poi[lon_col]) & np.isfinite(poi[lat_col])].copy()
    poi_gdf = gpd.GeoDataFrame(
        poi[[category_col]].copy(),
        geometry=[Point(xy) for xy in zip(poi[lon_col], poi[lat_col])],
        crs=config["crs"]["geographic"],
    )
    poi_gdf = poi_gdf.to_crs(segments.crs)

    categories = sorted(str(value) for value in poi_gdf[category_col].dropna().unique())
    category_to_col = {category: f"poi_cat_{idx:02d}" for idx, category in enumerate(categories)}
    poi_gdf["poi_category_col"] = poi_gdf[category_col].astype(str).map(category_to_col)

    buffers = segments[["seg_id", "geometry"]].copy()
    buffers["geometry"] = buffers.geometry.buffer(float(poi_config["buffer_m"]))

    print("Assigning POIs to segment buffers...")
    joined = gpd.sjoin(
        poi_gdf[["poi_category_col", "geometry"]],
        buffers,
        how="inner",
        predicate="within",
    )

    category_counts = (
        joined.groupby(["seg_id", "poi_category_col"])
        .size()
        .unstack(fill_value=0)
        .reindex(index=segment_ids, columns=list(category_to_col.values()), fill_value=0)
    )
    category_counts = category_counts.astype(int)

    features = pd.DataFrame({"seg_id": segment_ids})
    features = features.merge(category_counts.reset_index(), on="seg_id", how="left").fillna(0)
    category_cols = list(category_to_col.values())
    features[category_cols] = features[category_cols].astype(int)
    features["poi_total"] = features[category_cols].sum(axis=1).astype(int)

    lengths = segments[["seg_id", "length"]].copy()
    features = features.merge(lengths, on="seg_id", how="left")
    length_km = features["length"].astype(float).clip(lower=1.0) / 1000.0
    features["poi_density"] = features["poi_total"] / length_km
    features["poi_entropy"] = [
        normalized_entropy(row)
        for row in features[category_cols].to_numpy().astype(float)
    ]

    dominant_cols = features[category_cols].idxmax(axis=1)
    no_poi_mask = features["poi_total"] == 0
    col_to_category = {column: category for category, column in category_to_col.items()}
    features["dominant_poi_type"] = dominant_cols.map(col_to_category)
    features.loc[no_poi_mask, "dominant_poi_type"] = None
    features = features.drop(columns=["length"])

    category_map = pd.DataFrame(
        [{"category_col": column, "poi_type": category} for category, column in category_to_col.items()]
    )
    features.to_csv(output_path, index=False)
    category_map.to_csv(category_map_path, index=False)

    print(f"number of ordinary segments: {len(segment_ids):,}")
    print(f"number of loaded POIs with valid coordinates: {len(poi_gdf):,}")
    print(f"number of segment-buffer POI assignments: {len(joined):,}")
    print(f"number of segments without POIs in buffer: {int(no_poi_mask.sum()):,}")
    print(f"Saved segment POI features to {output_path}")
    print(f"Saved POI category mapping to {category_map_path}")
if __name__ == "__main__":
    main()
