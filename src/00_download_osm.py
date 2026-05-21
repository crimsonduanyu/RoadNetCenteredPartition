from __future__ import annotations

import inspect

import env_setup  # noqa: F401
import geopandas as gpd
import osmnx as ox

from utils_geo import (
    build_center_point,
    build_inner_polygon_from_ring_buffer,
    ensure_scope_directories,
    get_active_scope,
    get_scope_paths,
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
    bbox = get_active_scope(config)["harvest_bbox"]
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


def extract_ring_segments(edges: gpd.GeoDataFrame, config: dict) -> gpd.GeoDataFrame:
    if "name" not in edges.columns:
        raise ValueError("Input graph edges have no 'name' column; cannot identify configured ring segments.")

    scope = get_active_scope(config)
    include_patterns = [normalize_road_name(name) for name in scope["ring_name_patterns"]]
    exclude_patterns = [normalize_road_name(name) for name in scope.get("exclude_name_patterns", [])]
    edges = edges.loc[edges.geometry.notna()].copy()
    edges["normalized_name"] = edges["name"].map(normalize_road_name)
    ring_name_mask = edges["name"].map(lambda value: road_name_matches(value, include_patterns, exclude_patterns))
    ring_segments = edges.loc[ring_name_mask].copy()
    ring_segments = ring_segments.loc[ring_segments.geometry.geom_type.isin(["LineString", "MultiLineString"])].copy()

    if ring_segments.empty:
        raise ValueError(f"No {scope['label']} candidate segments matched the configured road names.")

    return ring_segments


def validate_ring_segments(ring_segments: gpd.GeoDataFrame, config: dict) -> None:
    scope = get_active_scope(config)
    min_segment_count = int(scope.get("min_ring_segment_count", 0))
    min_total_length_km = float(scope.get("min_ring_total_length_km", 0))
    min_span_km = float(scope.get("min_ring_span_km", 0))

    segment_count = len(ring_segments)
    total_length_km = float(ring_segments.geometry.length.sum()) / 1_000
    minx, miny, maxx, maxy = ring_segments.total_bounds
    span_x_km = float(maxx - minx) / 1_000
    span_y_km = float(maxy - miny) / 1_000

    if segment_count < min_segment_count:
        raise ValueError(
            f"{scope['label']} extraction matched too few road segments "
            f"({segment_count:,} < {min_segment_count:,}). Check ring_name_patterns."
        )
    if total_length_km < min_total_length_km:
        raise ValueError(
            f"{scope['label']} extraction matched too little linework "
            f"({total_length_km:,.1f} km < {min_total_length_km:,.1f} km)."
        )
    if span_x_km < min_span_km or span_y_km < min_span_km:
        raise ValueError(
            f"{scope['label']} extraction is spatially too small "
            f"(span {span_x_km:,.1f} km x {span_y_km:,.1f} km; minimum {min_span_km:,.1f} km)."
        )


def validate_boundary_area(boundary: gpd.GeoDataFrame, config: dict) -> None:
    scope = get_active_scope(config)
    min_area_km2 = float(scope.get("min_boundary_area_km2", 0))
    max_area_km2 = float(scope.get("max_boundary_area_km2", float("inf")))
    area_km2 = float(boundary.geometry.area.iloc[0]) / 1_000_000

    if area_km2 < min_area_km2 or area_km2 > max_area_km2:
        raise ValueError(
            f"Selected {scope['label']} boundary area is outside the expected range "
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


def build_boundary_from_ring_segments(ring_segments: gpd.GeoDataFrame, config: dict) -> gpd.GeoDataFrame:
    scope = get_active_scope(config)
    polygons = polygonize_ring_lines(ring_segments)
    print(f"number of boundary polygons generated before selection: {len(polygons):,}")

    center_point = build_center_point(config)
    if polygons.empty:
        print("Polygonization returned no boundary polygons; falling back to buffered ring extraction.")
        return build_inner_polygon_from_ring_buffer(
            ring_segments,
            center_point,
            scope["harvest_bbox"],
            config["crs"]["geographic"],
            float(scope["boundary_buffer_m"]),
        )

    try:
        return select_polygon_containing_point(polygons, center_point)
    except ValueError:
        print("No polygonized candidate contained the center point; falling back to buffered ring extraction.")
        return build_inner_polygon_from_ring_buffer(
            ring_segments,
            center_point,
            scope["harvest_bbox"],
            config["crs"]["geographic"],
            float(scope["boundary_buffer_m"]),
        )


def save_boundary_assets_from_edges(edges: gpd.GeoDataFrame, config: dict) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    scope = get_active_scope(config)
    paths = get_scope_paths(config)

    ring_segments = extract_ring_segments(edges, config)
    print(f"number of candidate {scope['label']} segments found: {len(ring_segments):,}")
    ring_segments = ring_segments.reset_index(drop=False)
    ring_segments = project_gdf(ring_segments, config["crs"]["projected"]).copy()
    ring_segments = select_existing_columns(
        ring_segments,
        ["u", "v", "key", "osmid", "name", "highway", "length", "ref", "normalized_name", "geometry"],
    )
    validate_ring_segments(ring_segments, config)
    make_gpkg_safe(ring_segments).to_file(paths["ring_segments"], driver="GPKG")

    boundary = build_boundary_from_ring_segments(ring_segments, config)
    validate_boundary_polygon(boundary)
    validate_boundary_area(boundary, config)
    make_gpkg_safe(boundary).to_file(paths["boundary"], driver="GPKG")

    chosen_area_km2 = float(boundary["area_m2"].iloc[0]) / 1_000_000
    print(f"chosen boundary polygon area (km^2): {chosen_area_km2:,.2f}")
    return ring_segments, boundary


def main() -> None:
    config = load_config()
    ensure_scope_directories(config)
    scope = get_active_scope(config)
    paths = get_scope_paths(config)

    if scope["name"] != "fifth_ring":
        print(f"Building {scope['label']} boundary from shared raw edges at {paths['raw_edges']}...")
        raw_edges = gpd.read_file(paths["raw_edges"])
        save_boundary_assets_from_edges(raw_edges, config)
        print(f"Saved {scope['label']} segments to {paths['ring_segments']}")
        print(f"Saved {scope['label']} boundary to {paths['boundary']}")
        return

    if paths["raw_edges"].exists() and paths["raw_nodes"].exists() and paths["raw_graphml"].exists():
        if not paths["ring_segments"].exists() or not paths["boundary"].exists():
            print(f"Rebuilding missing {scope['label']} boundary assets from shared raw edges at {paths['raw_edges']}...")
            raw_edges = gpd.read_file(paths["raw_edges"])
            save_boundary_assets_from_edges(raw_edges, config)
        else:
            print(f"Reusing existing shared raw {scope['label']} network at {paths['raw_edges']}.")
        return

    configure_osmnx_tags()

    print(f"Downloading drivable graph in a broad harvest window for {scope['label']} boundary extraction...")
    harvest_graph = harvest_drive_graph(config)
    harvest_nodes, harvest_edges = ox.graph_to_gdfs(harvest_graph)
    _, boundary = save_boundary_assets_from_edges(harvest_edges, config)

    boundary_wgs84 = boundary.to_crs(config["crs"]["geographic"])

    print(f"Downloading Beijing drivable road network within the {scope['label']} polygon...")
    graph = graph_from_polygon(boundary_wgs84.geometry.iloc[0])
    nodes, edges = ox.graph_to_gdfs(graph)

    safe_edges = select_existing_columns(
        ensure_columns(edges.reset_index(drop=False), REQUIRED_WAY_TAGS),
        ["u", "v", "key", "osmid", "name", "highway", "oneway", "length", "bridge", "tunnel", "access", "vehicle", "motor_vehicle", "motorcar", "taxi", "psv", "bus", "service", "foot", "bicycle", "ref", "lanes", "maxspeed", "junction", "geometry"],
    )
    safe_nodes = select_existing_columns(
        nodes.reset_index(drop=False),
        ["osmid", "y", "x", "street_count", "highway", "junction", "railway", "geometry"],
    )

    ox.save_graphml(graph, paths["raw_graphml"])
    make_gpkg_safe(safe_edges).to_file(paths["raw_edges"], driver="GPKG")
    make_gpkg_safe(safe_nodes).to_file(paths["raw_nodes"], driver="GPKG")

    print(f"Saved {scope['label']} segments to {paths['ring_segments']}")
    print(f"Saved {scope['label']} boundary to {paths['boundary']}")
    print(f"Saved graph to {paths['raw_graphml']}")
    print(f"Saved {len(edges):,} edges to {paths['raw_edges']}")
    print(f"Saved {len(nodes):,} nodes to {paths['raw_nodes']}")


if __name__ == "__main__":
    main()
