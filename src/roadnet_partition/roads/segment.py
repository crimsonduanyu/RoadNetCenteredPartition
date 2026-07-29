"""Road-segment helpers retained from the historical geospatial module."""

from roadnet_partition.io.geospatial import (
    angle_diff,
    compute_bearing,
    make_gpkg_safe,
    normalize_columns,
    normalize_osm_value,
    normalize_road_name,
    road_name_matches,
    select_existing_columns,
)

__all__ = [
    "normalize_osm_value",
    "normalize_columns",
    "compute_bearing",
    "angle_diff",
    "normalize_road_name",
    "road_name_matches",
    "select_existing_columns",
    "make_gpkg_safe",
]
