from __future__ import annotations

from pathlib import Path
from typing import Any

import env_setup  # noqa: F401
import geopandas as gpd
import numpy as np
import yaml
from shapely.geometry import Point, Polygon, box
from shapely.ops import linemerge, polygonize, unary_union


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = PROJECT_ROOT / "data" / "raw"
DATA_INTERIM = PROJECT_ROOT / "data" / "interim"
DATA_PROCESSED = PROJECT_ROOT / "data" / "processed"
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"


REQUIRED_DIRS = [
    DATA_RAW,
    DATA_INTERIM,
    DATA_PROCESSED,
    OUTPUTS_ROOT,
]


OSM_NORMALIZE_FIELDS = [
    "highway",
    "name",
    "osmid",
    "oneway",
    "bridge",
    "tunnel",
    "access",
    "service",
    "vehicle",
    "motor_vehicle",
    "motorcar",
    "taxi",
    "psv",
    "bus",
    "foot",
    "bicycle",
]


def load_config() -> dict[str, Any]:
    with (PROJECT_ROOT / "config.yaml").open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def get_active_scope_name(config: dict[str, Any]) -> str:
    study_area = config["study_area"]
    return str(study_area.get("active", study_area.get("boundary_source", "fifth_ring")))


def get_active_scope(config: dict[str, Any]) -> dict[str, Any]:
    study_area = config["study_area"]
    active = get_active_scope_name(config)
    scopes = study_area.get("scopes")
    if scopes:
        if active not in scopes:
            raise ValueError(f"Unknown study_area.active '{active}'. Expected one of {list(scopes)}.")
        scope = dict(scopes[active])
        scope["name"] = active
    else:
        scope = dict(study_area)
        scope["name"] = active
        scope.setdefault("label", active)

    scope.setdefault("label", active)
    scope.setdefault("raw_edges_path", "data/raw/beijing_edges_raw.gpkg")
    scope.setdefault("raw_nodes_path", "data/raw/beijing_nodes_raw.gpkg")
    scope.setdefault("graphml_path", "data/raw/beijing_drive_within_fifth_ring.graphml")
    scope.setdefault("boundary_path", f"data/raw/beijing_{active}_boundary.gpkg")
    scope.setdefault("ring_segments_path", f"data/raw/beijing_{active}_segments.gpkg")
    scope.setdefault("retain_boundary_roads", True)
    return scope


def project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def get_scope_directories(config: dict[str, Any]) -> dict[str, Path]:
    scope_name = get_active_scope_name(config)
    return {
        "data_interim": DATA_INTERIM / scope_name,
        "data_processed": DATA_PROCESSED / scope_name,
        "outputs_figures": PROJECT_ROOT / "outputs" / scope_name / "figures",
        "outputs_graphs": PROJECT_ROOT / "outputs" / scope_name / "graphs",
        "outputs_tables": PROJECT_ROOT / "outputs" / scope_name / "tables",
    }


def get_scope_paths(config: dict[str, Any]) -> dict[str, Path]:
    scope = get_active_scope(config)
    dirs = get_scope_directories(config)
    return {
        **dirs,
        "raw_edges": project_path(scope["raw_edges_path"]),
        "raw_nodes": project_path(scope["raw_nodes_path"]),
        "raw_graphml": project_path(scope["graphml_path"]),
        "boundary": project_path(scope["boundary_path"]),
        "ring_segments": project_path(scope["ring_segments_path"]),
        "classified_edges": dirs["data_interim"] / "road_edges_classified.gpkg",
        "segment_nodes": dirs["data_processed"] / "segment_nodes.gpkg",
        "poi_features": dirs["data_processed"] / "segment_poi_features.csv",
        "poi_category_mapping": dirs["data_processed"] / "poi_category_mapping.csv",
        "order_features": dirs["data_processed"] / "segment_order_features.csv",
        "order_od_pairs": dirs["data_processed"] / "segment_order_od_pairs.csv",
        "order_od_hourly": dirs["data_processed"] / "segment_order_od_hourly.csv",
    }


def ensure_directories() -> None:
    for directory in REQUIRED_DIRS:
        directory.mkdir(parents=True, exist_ok=True)



def ensure_scope_directories(config: dict[str, Any]) -> None:
    ensure_directories()
    for directory in get_scope_directories(config).values():
        directory.mkdir(parents=True, exist_ok=True)


def normalize_osm_value(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def normalize_columns(gdf, columns: list[str]):
    for column in columns:
        if column in gdf.columns:
            gdf[column] = gdf[column].map(normalize_osm_value)
    return gdf


def project_gdf(gdf, target_crs: str):
    if gdf.crs is None:
        raise ValueError("GeoDataFrame has no CRS; cannot project.")
    if str(gdf.crs) == target_crs:
        return gdf
    return gdf.to_crs(target_crs)


def compute_bearing(line) -> float | None:
    if line is None or line.is_empty:
        return None
    coords = list(line.coords)
    if len(coords) < 2:
        return None
    x1, y1 = coords[0]
    x2, y2 = coords[-1]
    angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
    return float(angle % 180)


def angle_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    diff = abs(a - b) % 180
    return float(min(diff, 180 - diff))


def project_bounds(bounds: dict[str, float], source_crs: str, target_crs: str) -> dict[str, float]:
    bbox = gpd.GeoSeries([box(bounds["west"], bounds["south"], bounds["east"], bounds["north"])], crs=source_crs)
    projected = bbox.to_crs(target_crs).total_bounds
    minx, miny, maxx, maxy = projected
    return {
        "west": float(minx),
        "south": float(miny),
        "east": float(maxx),
        "north": float(maxy),
    }


def normalize_road_name(value: Any) -> str:
    value = normalize_osm_value(value)
    if value is None:
        return ""
    return str(value).strip().replace(" ", "")


def road_name_matches(value: Any, include_patterns: list[str], exclude_patterns: list[str] | None = None) -> bool:
    normalized = normalize_road_name(value)
    if not normalized:
        return False
    if not any(pattern in normalized for pattern in include_patterns):
        return False
    if exclude_patterns and any(pattern in normalized for pattern in exclude_patterns):
        return False
    return True


def build_center_point(config: dict) -> gpd.GeoSeries:
    center = config["study_area"]["center_point"]
    return gpd.GeoSeries([Point(center["lon"], center["lat"])], crs=config["crs"]["geographic"])


def build_harvest_polygon(bounds: dict[str, float], crs: str):
    return gpd.GeoSeries([box(bounds["west"], bounds["south"], bounds["east"], bounds["north"])], crs=crs)


def polygonize_ring_lines(ring_lines_gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    merged = unary_union([geom for geom in ring_lines_gdf.geometry if geom is not None and not geom.is_empty])
    merged = linemerge(merged)
    polygons = list(polygonize(merged))
    return gpd.GeoDataFrame({"geometry": polygons}, crs=ring_lines_gdf.crs)


def build_inner_polygon_from_ring_buffer(
    ring_lines_gdf: gpd.GeoDataFrame,
    point_gs: gpd.GeoSeries,
    harvest_bounds: dict[str, float],
    geographic_crs: str,
    buffer_m: float,
) -> gpd.GeoDataFrame:
    harvest_polygon = build_harvest_polygon(harvest_bounds, geographic_crs).to_crs(ring_lines_gdf.crs)
    corridor = unary_union(ring_lines_gdf.buffer(buffer_m, cap_style=2, join_style=2))
    remaining = harvest_polygon.iloc[0].difference(corridor)

    if remaining.is_empty:
        raise ValueError("Buffered ring corridor removed the entire harvest area.")

    if remaining.geom_type == "Polygon":
        polygon_geoms = [remaining]
    else:
        polygon_geoms = [geom for geom in getattr(remaining, "geoms", []) if isinstance(geom, Polygon)]

    polygons = gpd.GeoDataFrame({"geometry": polygon_geoms}, crs=ring_lines_gdf.crs)
    return select_polygon_containing_point(polygons, point_gs)


def select_polygon_containing_point(polygons_gdf: gpd.GeoDataFrame, point_gs: gpd.GeoSeries) -> gpd.GeoDataFrame:
    point_projected = point_gs.to_crs(polygons_gdf.crs)
    selected = polygons_gdf.loc[polygons_gdf.contains(point_projected.iloc[0])].copy()
    if selected.empty:
        raise ValueError("No boundary polygon contains the configured center point.")
    selected["area_m2"] = selected.geometry.area.astype(float)
    selected = selected.sort_values("area_m2", ascending=True).head(1).copy()
    return selected


def validate_boundary_polygon(boundary_gdf: gpd.GeoDataFrame) -> None:
    if boundary_gdf.empty:
        raise ValueError("Boundary polygon GeoDataFrame is empty.")
    if len(boundary_gdf) != 1:
        raise ValueError(f"Expected exactly one boundary polygon, found {len(boundary_gdf)}.")
    geometry = boundary_gdf.geometry.iloc[0]
    if geometry is None or geometry.is_empty:
        raise ValueError("Boundary polygon geometry is empty.")
    if not geometry.is_valid:
        raise ValueError("Boundary polygon is invalid.")


def select_existing_columns(gdf: gpd.GeoDataFrame, columns: list[str]) -> gpd.GeoDataFrame:
    keep = [column for column in columns if column in gdf.columns]
    if "geometry" not in keep:
        keep.append("geometry")
    return gdf.loc[:, keep].copy()


def make_gpkg_safe(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    safe = gdf.copy()
    used_names: dict[str, int] = {}
    renamed = []
    for column in safe.columns:
        if column == "geometry":
            renamed.append(column)
            continue
        candidate = str(column).replace(":", "_").replace("-", "_")
        normalized = candidate.lower()
        if normalized in used_names:
            used_names[normalized] += 1
            candidate = f"{candidate}_{used_names[normalized]}"
        else:
            used_names[normalized] = 0
        renamed.append(candidate)
    safe.columns = renamed

    for column in safe.columns:
        if column == "geometry":
            continue
        safe[column] = safe[column].map(normalize_osm_value)
        sample = safe[column].dropna()
        if not sample.empty and isinstance(sample.iloc[0], (list, dict, set, tuple)):
            safe[column] = safe[column].map(lambda value: None if value is None else str(value))
    return safe
