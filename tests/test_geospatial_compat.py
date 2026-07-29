from __future__ import annotations

import os

import geopandas as gpd
from shapely.geometry import LineString, Point

import env_setup
from lib import geo as legacy
from roadnet_partition.io import geospatial as current
from roadnet_partition.io.environment import initialize_geospatial_environment
from roadnet_partition.roads import segment


PUBLIC_NAMES = {
    "PROJECT_ROOT", "DATA_RAW", "DATA_INTERIM", "DATA_PROCESSED", "OUTPUTS_ROOT",
    "REQUIRED_DIRS", "OSM_NORMALIZE_FIELDS", "load_config", "get_active_scope_name",
    "get_active_scope", "project_path", "get_scope_directories", "get_scope_paths",
    "ensure_directories", "ensure_scope_directories", "normalize_osm_value",
    "normalize_columns", "project_gdf", "compute_bearing", "angle_diff",
    "project_bounds", "normalize_road_name", "road_name_matches", "build_center_point",
    "build_harvest_polygon", "polygonize_ring_lines", "build_inner_polygon_from_ring_buffer",
    "select_polygon_containing_point", "validate_boundary_polygon", "select_existing_columns",
    "make_gpkg_safe",
}


def test_legacy_geospatial_exports_are_compatibility_aliases() -> None:
    assert set(legacy.__all__) == PUBLIC_NAMES
    for name in PUBLIC_NAMES:
        assert getattr(legacy, name) is getattr(current, name)


def test_representative_crs_and_geometry_helpers_preserve_outputs() -> None:
    frame = gpd.GeoDataFrame(
        {"seg_id": ["a"], "items": [[1, 2]]},
        geometry=[Point(116.4, 39.9)],
        crs="EPSG:4326",
    )
    projected = current.project_gdf(frame, "EPSG:32650")
    assert projected.crs.to_string() == "EPSG:32650"
    assert legacy.project_gdf(frame, "EPSG:32650").geometry.equals(projected.geometry)

    line = LineString([(0, 0), (1, 1)])
    assert current.compute_bearing(line) == legacy.compute_bearing(line) == 45.0
    assert current.angle_diff(170.0, 10.0) == legacy.angle_diff(170.0, 10.0) == 20.0
    safe = current.make_gpkg_safe(frame)
    assert list(safe.columns) == list(legacy.make_gpkg_safe(frame).columns)
    assert safe["items"].tolist() == [1]


def test_segment_module_reexports_clear_segment_helpers() -> None:
    assert segment.compute_bearing is current.compute_bearing
    assert segment.normalize_road_name is current.normalize_road_name
    assert segment.road_name_matches(" 东五环 ", ["东五环"], ["辅路"])


def test_geospatial_environment_initialization_is_repeatable() -> None:
    before = os.environ.get("GDAL_DATA")
    initialize_geospatial_environment()
    initialize_geospatial_environment()
    assert os.environ.get("GDAL_DATA") == before
    assert env_setup.conda_prefix
    assert hasattr(env_setup, "gdal_data") == ("gdal_data" in env_setup.__all__)
