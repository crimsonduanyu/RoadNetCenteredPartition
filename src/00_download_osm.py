from __future__ import annotations

import inspect

import env_setup  # noqa: F401
import geopandas as gpd
import osmnx as ox

from utils_geo import (
    DATA_RAW,
    build_center_point,
    build_inner_polygon_from_ring_buffer,
    ensure_directories,
    load_config,
    make_gpkg_safe,
    normalize_road_name,
    polygonize_ring_lines,
    project_gdf,
    road_name_matches,
    select_existing_columns,
    select_polygon_containing_point,
    validate_boundary_polygon,
)


REQUIRED_WAY_TAGS = [
    "access",
    "vehicle",
    "motor_vehicle",
    "motorcar",
    "taxi",
    "psv",
    "bus",
    "service",
    "foot",
    "bicycle",
]


def configure_osmnx_tags() -> None:
    ox.settings.useful_tags_way = list(dict.fromkeys([*ox.settings.useful_tags_way, *REQUIRED_WAY_TAGS]))


def harvest_drive_graph(config: dict):
    bbox = config["study_area"]["harvest_bbox"]
    params = inspect.signature(ox.graph_from_bbox).parameters
    kwargs = {"network_type": "drive", "simplify": True}

    if "bbox" in params:
        return ox.graph_from_bbox(
            bbox=(bbox["west"], bbox["south"], bbox["east"], bbox["north"]),
            **kwargs,
        )

    return ox.graph_from_bbox(
        bbox["north"],
        bbox["south"],
        bbox["east"],
        bbox["west"],
        **kwargs,
    )


def extract_fifth_ring_segments(edges: gpd.GeoDataFrame, config: dict) -> gpd.GeoDataFrame:
    if "name" not in edges.columns:
        raise ValueError("Harvested graph edges have no 'name' column; cannot identify Fifth Ring segments.")

    include_patterns = [normalize_road_name(name) for name in config["study_area"]["ring_name_patterns"]]
    exclude_patterns = [normalize_road_name(name) for name in config["study_area"].get("exclude_name_patterns", [])]
    edges = edges.loc[edges.geometry.notna()].copy()
    edges["normalized_name"] = edges["name"].map(normalize_road_name)
    ring_name_mask = edges["name"].map(lambda value: road_name_matches(value, include_patterns, exclude_patterns))
    ring_segments = edges.loc[ring_name_mask].copy()
    ring_segments = ring_segments.loc[ring_segments.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()

    if ring_segments.empty:
        raise ValueError("No Fifth Ring candidate segments matched the configured road names.")

    return ring_segments


def validate_fifth_ring_segments(ring_segments: gpd.GeoDataFrame, config: dict) -> None:
    study_area = config["study_area"]
    min_segment_count = int(study_area.get("min_ring_segment_count", 0))
    min_total_length_km = float(study_area.get("min_ring_total_length_km", 0))
    min_span_km = float(study_area.get("min_ring_span_km", 0))

    segment_count = len(ring_segments)
    total_length_km = float(ring_segments.geometry.length.sum()) / 1_000
    minx, miny, maxx, maxy = ring_segments.total_bounds
    span_x_km = float(maxx - minx) / 1_000
    span_y_km = float(maxy - miny) / 1_000

    if segment_count < min_segment_count:
        raise ValueError(
            "Fifth Ring extraction matched too few road segments "
            f"({segment_count:,} < {min_segment_count:,}). Check ring_name_patterns."
        )
    if total_length_km < min_total_length_km:
        raise ValueError(
            "Fifth Ring extraction matched too little linework "
            f"({total_length_km:,.1f} km < {min_total_length_km:,.1f} km)."
        )
    if span_x_km < min_span_km or span_y_km < min_span_km:
        raise ValueError(
            "Fifth Ring extraction is spatially too small "
            f"(span {span_x_km:,.1f} km x {span_y_km:,.1f} km; minimum {min_span_km:,.1f} km)."
        )


def validate_boundary_area(boundary: gpd.GeoDataFrame, config: dict) -> None:
    study_area = config["study_area"]
    min_area_km2 = float(study_area.get("min_boundary_area_km2", 0))
    max_area_km2 = float(study_area.get("max_boundary_area_km2", float("inf")))
    area_km2 = float(boundary.geometry.area.iloc[0]) / 1_000_000

    if area_km2 < min_area_km2 or area_km2 > max_area_km2:
        raise ValueError(
            "Selected Fifth Ring boundary area is outside the expected range "
            f"({area_km2:,.2f} km^2; expected {min_area_km2:,.2f}-{max_area_km2:,.2f} km^2)."
        )


def graph_from_polygon(polygon_wgs84):
    return ox.graph_from_polygon(polygon_wgs84, network_type="drive", simplify=True)


def ensure_columns(gdf: gpd.GeoDataFrame, columns: list[str]) -> gpd.GeoDataFrame:
    gdf = gdf.copy()
    for column in columns:
        if column not in gdf.columns:
            gdf[column] = None
    return gdf


def main() -> None:
    ensure_directories()
    configure_osmnx_tags()
    config = load_config()

    print("Downloading drivable graph in a broad harvest window for Fifth Ring boundary extraction...")
    harvest_graph = harvest_drive_graph(config)
    harvest_nodes, harvest_edges = ox.graph_to_gdfs(harvest_graph)
    ring_segments = extract_fifth_ring_segments(harvest_edges, config)
    print(f"number of candidate Fifth Ring segments found: {len(ring_segments):,}")

    ring_segments = ring_segments.reset_index(drop=False)
    ring_segments = project_gdf(ring_segments, config["crs"]["projected"]).copy()
    ring_segments = select_existing_columns(
        ring_segments,
        ["u", "v", "key", "osmid", "name", "highway", "length", "ref", "normalized_name", "geometry"],
    )
    validate_fifth_ring_segments(ring_segments, config)

    ring_segments_path = DATA_RAW / "beijing_fifth_ring_segments.gpkg"
    make_gpkg_safe(ring_segments).to_file(ring_segments_path, driver="GPKG")

    polygons = polygonize_ring_lines(ring_segments)
    print(f"number of boundary polygons generated before selection: {len(polygons):,}")

    center_point = build_center_point(config)
    if polygons.empty:
        print("Polygonization returned no boundary polygons; falling back to buffered ring extraction.")
        boundary = build_inner_polygon_from_ring_buffer(
            ring_segments,
            center_point,
            config["study_area"]["harvest_bbox"],
            config["crs"]["geographic"],
            float(config["study_area"]["boundary_buffer_m"]),
        )
    else:
        try:
            boundary = select_polygon_containing_point(polygons, center_point)
        except ValueError:
            print("No polygonized candidate contained the center point; falling back to buffered ring extraction.")
            boundary = build_inner_polygon_from_ring_buffer(
                ring_segments,
                center_point,
                config["study_area"]["harvest_bbox"],
                config["crs"]["geographic"],
                float(config["study_area"]["boundary_buffer_m"]),
            )
    validate_boundary_polygon(boundary)
    validate_boundary_area(boundary, config)

    boundary_path = DATA_RAW / "beijing_fifth_ring_boundary.gpkg"
    make_gpkg_safe(boundary).to_file(boundary_path, driver="GPKG")

    boundary_wgs84 = boundary.to_crs(config["crs"]["geographic"])
    chosen_area_km2 = float(boundary["area_m2"].iloc[0]) / 1_000_000
    print(f"chosen boundary polygon area (km^2): {chosen_area_km2:,.2f}")

    print("Downloading Beijing drivable road network within the Fifth Ring polygon...")
    graph = graph_from_polygon(boundary_wgs84.geometry.iloc[0])
    nodes, edges = ox.graph_to_gdfs(graph)

    graphml_path = DATA_RAW / "beijing_drive_within_fifth_ring.graphml"
    edges_path = DATA_RAW / "beijing_edges_raw.gpkg"
    nodes_path = DATA_RAW / "beijing_nodes_raw.gpkg"

    safe_edges = select_existing_columns(
        ensure_columns(edges.reset_index(drop=False), REQUIRED_WAY_TAGS),
        ["u", "v", "key", "osmid", "name", "highway", "oneway", "length", "bridge", "tunnel", "access", "vehicle", "motor_vehicle", "motorcar", "taxi", "psv", "bus", "service", "foot", "bicycle", "ref", "lanes", "maxspeed", "junction", "geometry"],
    )
    safe_nodes = select_existing_columns(
        nodes.reset_index(drop=False),
        ["osmid", "y", "x", "street_count", "highway", "junction", "railway", "geometry"],
    )

    ox.save_graphml(graph, graphml_path)
    make_gpkg_safe(safe_edges).to_file(edges_path, driver="GPKG")
    make_gpkg_safe(safe_nodes).to_file(nodes_path, driver="GPKG")

    print(f"Saved Fifth Ring segments to {ring_segments_path}")
    print(f"Saved Fifth Ring boundary to {boundary_path}")
    print(f"Saved graph to {graphml_path}")
    print(f"Saved {len(edges):,} edges to {edges_path}")
    print(f"Saved {len(nodes):,} nodes to {nodes_path}")


if __name__ == "__main__":
    main()
