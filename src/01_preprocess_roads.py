from __future__ import annotations

import env_setup  # noqa: F401
import geopandas as gpd
import pandas as pd
from collections import Counter

from utils_geo import (
    DATA_INTERIM,
    DATA_RAW,
    OSM_NORMALIZE_FIELDS,
    ensure_directories,
    load_config,
    normalize_columns,
    normalize_road_name,
    project_gdf,
    road_name_matches,
    validate_boundary_polygon,
)


ACCESS_COLUMNS = ["access", "vehicle", "motor_vehicle", "psv", "motorcar", "taxi"]


def normalize_highway(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def normalized_string(value):
    value = normalize_highway(value)
    if value is None or pd.isna(value):
        return None
    return str(value).strip().lower()


def split_access_values(value) -> list[str]:
    value = normalized_string(value)
    if not value:
        return []
    values = []
    for part in str(value).replace("|", ";").split(";"):
        part = part.strip()
        if part:
            values.append(part)
    return values


def length_ratio_inside(geometry, polygon) -> float:
    if geometry is None or geometry.is_empty:
        return 0.0
    length = float(geometry.length)
    if length == 0:
        return 0.0
    return float(geometry.intersection(polygon).length) / length


def is_fifth_ring_name(value, config: dict) -> bool:
    include_patterns = [normalize_road_name(name) for name in config["study_area"]["ring_name_patterns"]]
    exclude_patterns = [normalize_road_name(name) for name in config["study_area"].get("exclude_name_patterns", [])]
    return road_name_matches(value, include_patterns, exclude_patterns)


def build_scope_mask(edges: gpd.GeoDataFrame, boundary: gpd.GeoDataFrame, ring_segments: gpd.GeoDataFrame, config: dict) -> tuple[pd.Series, pd.Series, pd.Series]:
    study_area = config["study_area"]
    threshold = float(study_area.get("inside_length_ratio_threshold", 0.98))
    boundary_tolerance_m = float(study_area.get("boundary_tolerance_m", 0))
    ring_overlap_tolerance_m = float(study_area.get("ring_overlap_tolerance_m", 0))

    boundary_polygon = boundary.geometry.iloc[0]
    inside_polygon = boundary_polygon.buffer(boundary_tolerance_m) if boundary_tolerance_m else boundary_polygon
    ring_linework = ring_segments.geometry.union_all()
    ring_corridor = ring_linework.buffer(ring_overlap_tolerance_m) if ring_overlap_tolerance_m else ring_linework

    inside_ratio = edges.geometry.map(lambda geometry: length_ratio_inside(geometry, inside_polygon))
    inside_mask = inside_ratio >= threshold
    ring_name_mask = edges["name"].map(lambda value: is_fifth_ring_name(value, config)) if "name" in edges.columns else pd.Series(False, index=edges.index)
    ring_overlap_mask = edges.geometry.intersects(ring_corridor)
    ring_mask = ring_name_mask & ring_overlap_mask
    return inside_mask | ring_mask, inside_mask, ring_mask


def evaluate_access_record(record, allowed_values: set[str], excluded_values: set[str], designated_columns: set[str]) -> tuple[bool, str | None]:
    has_access_tag = False
    allowed = True
    rejection_reason = None

    for column in ACCESS_COLUMNS:
        if column not in record.index:
            continue
        values = split_access_values(record[column])
        if not values:
            continue

        has_access_tag = True
        denied_matches = [value for value in values if value in excluded_values]
        allowed_matches = [value for value in values if value in allowed_values]
        designated_allowed = column in designated_columns and "designated" in values

        if denied_matches:
            allowed = False
            rejection_reason = f"{column}={denied_matches[0]}"
        elif allowed_matches or designated_allowed:
            allowed = True
            rejection_reason = None
        else:
            allowed = False
            rejection_reason = f"{column}={values[0]}"

    if not has_access_tag:
        return True, None
    return allowed, rejection_reason


def access_allowed_mask(
    edges: gpd.GeoDataFrame,
    allowed_values: set[str],
    excluded_values: set[str],
    designated_columns: set[str],
) -> tuple[pd.Series, Counter]:
    decisions = edges.apply(
        lambda record: evaluate_access_record(record, allowed_values, excluded_values, designated_columns),
        axis=1,
    )
    allowed = decisions.map(lambda decision: decision[0])
    reject_counts = Counter(decision[1] for decision in decisions if decision[1])
    return allowed, reject_counts


def service_allowed_mask(edges: gpd.GeoDataFrame, excluded_values: set[str]) -> pd.Series:
    if "service" not in edges.columns:
        return pd.Series(True, index=edges.index)
    service_values = edges["service"].map(normalized_string)
    return ~((edges["highway"] == "service") & service_values.isin(excluded_values))


def format_counter(counter: Counter, limit: int = 10) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{key}: {value:,}" for key, value in counter.most_common(limit))


def main() -> None:
    ensure_directories()
    config = load_config()

    edges_path = DATA_RAW / "beijing_edges_raw.gpkg"
    boundary_path = DATA_RAW / "beijing_fifth_ring_boundary.gpkg"
    ring_segments_path = DATA_RAW / "beijing_fifth_ring_segments.gpkg"
    output_path = DATA_INTERIM / "road_edges_classified.gpkg"

    print(f"Loading raw edges from {edges_path}...")
    edges = gpd.read_file(edges_path)
    boundary = gpd.read_file(boundary_path)
    ring_segments = gpd.read_file(ring_segments_path)
    validate_boundary_polygon(boundary)

    raw_edge_count = len(edges)

    normalize_fields = list(dict.fromkeys(OSM_NORMALIZE_FIELDS))
    edges = normalize_columns(edges, normalize_fields)
    edges = project_gdf(edges, config["crs"]["projected"]).copy()
    boundary = project_gdf(boundary, config["crs"]["projected"]).copy()
    ring_segments = project_gdf(ring_segments, config["crs"]["projected"]).copy()
    edges["highway"] = edges["highway"].map(normalize_highway)
    edges["length"] = edges.geometry.length.astype(float)

    scope_mask, inside_scope_mask, ring_scope_mask = build_scope_mask(edges, boundary, ring_segments, config)
    outside_scope_count = int((~scope_mask).sum())
    retained_inside_count = int((scope_mask & inside_scope_mask).sum())
    retained_ring_count = int((scope_mask & ring_scope_mask & ~inside_scope_mask).sum())
    edges = edges.loc[scope_mask].copy()

    road_filter = config["road_filter"]
    keep_highway = set(road_filter["keep_highway"])
    exclude_highway = set(road_filter.get("exclude_highway", []))
    excluded_access_values = set(road_filter.get("exclude_access_values", []))
    allowed_access_values = set(road_filter.get("allow_access_values", []))
    allow_designated_access_columns = set(road_filter.get("allow_designated_access_columns", []))
    excluded_service_values = set(road_filter.get("exclude_service_values", []))
    connector_highway = set(road_filter["connector_highway"])
    max_connector_length_m = float(config["connector_rules"]["max_connector_length_m"])

    before_highway_count = len(edges)
    edges = edges.loc[edges["highway"].isin(keep_highway) & ~edges["highway"].isin(exclude_highway)].copy()
    highway_filtered_count = before_highway_count - len(edges)

    before_service_count = len(edges)
    edges = edges.loc[service_allowed_mask(edges, excluded_service_values)].copy()
    service_filtered_count = before_service_count - len(edges)

    before_access_count = len(edges)
    access_mask, access_reject_counts = access_allowed_mask(
        edges,
        allowed_access_values,
        excluded_access_values,
        allow_designated_access_columns,
    )
    edges = edges.loc[access_mask].copy()
    access_filtered_count = before_access_count - len(edges)

    edges = edges.reset_index(drop=True)
    edges["seg_id"] = [f"seg_{idx:07d}" for idx in range(len(edges))]

    is_connector = edges["highway"].isin(connector_highway) & (edges["length"] <= max_connector_length_m)
    edges["segment_role"] = pd.Series("ordinary", index=edges.index)
    edges.loc[is_connector, "segment_role"] = "connector"

    edges.to_file(output_path, driver="GPKG")

    ordinary_count = int((edges["segment_role"] == "ordinary").sum())
    connector_count = int((edges["segment_role"] == "connector").sum())

    print(f"number of raw edges: {raw_edge_count:,}")
    print(f"number of edges retained by inside-Fifth-Ring rule: {retained_inside_count:,}")
    print(f"number of additional Fifth Ring boundary edges retained: {retained_ring_count:,}")
    print(f"number of edges discarded outside Fifth Ring: {outside_scope_count:,}")
    print(f"number of edges removed by highway class filter: {highway_filtered_count:,}")
    print(f"number of service edges removed by service subtype filter: {service_filtered_count:,}")
    print(f"number of edges removed by taxi/motor-vehicle access filter: {access_filtered_count:,}")
    print(f"top taxi/motor-vehicle access rejection reasons: {format_counter(access_reject_counts)}")
    print(f"number of Fifth-Ring-retained edges: {len(edges):,}")
    print(f"number of ordinary segments: {ordinary_count:,}")
    print(f"number of connector segments: {connector_count:,}")
    print(f"Saved classified edges to {output_path}")


if __name__ == "__main__":
    main()
