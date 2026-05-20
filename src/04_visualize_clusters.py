from __future__ import annotations

import pickle
import shutil
import sys

import env_setup  # noqa: F401
import geopandas as gpd
from PIL import Image, ImageDraw
from shapely.geometry import LineString, MultiLineString

from utils_geo import DATA_INTERIM, DATA_PROCESSED, DATA_RAW, OUTPUTS_FIGURES, OUTPUTS_GRAPHS, ensure_directories, load_config, project_bounds


TAB20 = [
    "#1f77b4",
    "#aec7e8",
    "#ff7f0e",
    "#ffbb78",
    "#2ca02c",
    "#98df8a",
    "#d62728",
    "#ff9896",
    "#9467bd",
    "#c5b0d5",
    "#8c564b",
    "#c49c94",
    "#e377c2",
    "#f7b6d2",
    "#7f7f7f",
    "#c7c7c7",
    "#bcbd22",
    "#dbdb8d",
    "#17becf",
    "#9edae5",
]


def line_width_px(linewidth_points: float, dpi: int, multiplier: float = 1.0) -> int:
    return max(1, int(round(linewidth_points * multiplier * dpi / 72)))


def iter_line_coords(geometry):
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, LineString):
        yield list(geometry.coords)
    elif isinstance(geometry, MultiLineString):
        for line in geometry.geoms:
            yield list(line.coords)
    elif hasattr(geometry, "boundary"):
        yield from iter_line_coords(geometry.boundary)


def rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    hex_color = hex_color.lstrip("#")
    return (
        int(hex_color[0:2], 16),
        int(hex_color[2:4], 16),
        int(hex_color[4:6], 16),
        alpha,
    )


def make_canvas(bounds: tuple[float, float, float, float], size_px: int) -> tuple[Image.Image, ImageDraw.ImageDraw, callable]:
    minx, miny, maxx, maxy = bounds
    width = maxx - minx
    height = maxy - miny
    if width <= 0 or height <= 0:
        raise ValueError("Cannot plot empty bounds.")

    padding = int(size_px * 0.025)
    drawable = size_px - 2 * padding
    scale = min(drawable / width, drawable / height)
    offset_x = padding + (drawable - width * scale) / 2
    offset_y = padding + (drawable - height * scale) / 2

    image = Image.new("RGBA", (size_px, size_px), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")

    def transform(x: float, y: float) -> tuple[float, float]:
        px = offset_x + (x - minx) * scale
        py = size_px - offset_y - (y - miny) * scale
        return px, py

    return image, draw, transform


def draw_lines(draw: ImageDraw.ImageDraw, geometries, transform, color, width: int) -> None:
    for geometry in geometries:
        for coords in iter_line_coords(geometry):
            if len(coords) < 2:
                continue
            draw.line([transform(x, y) for x, y, *_ in coords], fill=color, width=width)


def save_image(image: Image.Image, output_path) -> None:
    image.convert("RGB").save(output_path)


def make_cluster_color_map(cluster_ids: list[int]) -> dict[int, str]:
    return {cluster_id: TAB20[index % len(TAB20)] for index, cluster_id in enumerate(cluster_ids)}


def gdf_bounds(gdf: gpd.GeoDataFrame) -> tuple[float, float, float, float]:
    return tuple(float(value) for value in gdf.total_bounds)


def bounds_dict_to_tuple(bounds: dict[str, float]) -> tuple[float, float, float, float]:
    return bounds["west"], bounds["south"], bounds["east"], bounds["north"]


def plot_boundary(draw: ImageDraw.ImageDraw, boundary: gpd.GeoDataFrame, transform, dpi: int) -> None:
    draw_lines(draw, boundary.geometry, transform, rgba("#111111"), line_width_px(1.2, dpi))


def plot_classification(edges: gpd.GeoDataFrame, boundary: gpd.GeoDataFrame, output_path, linewidth: float, dpi: int) -> None:
    size_px = int(12 * dpi)
    image, draw, transform = make_canvas(gdf_bounds(boundary), size_px)
    ordinary = edges.loc[edges["segment_role"] == "ordinary"]
    connectors = edges.loc[edges["segment_role"] == "connector"]
    draw_lines(draw, ordinary.geometry, transform, rgba("#4a4a4a"), line_width_px(linewidth, dpi))
    draw_lines(draw, connectors.geometry, transform, rgba("#e66101"), line_width_px(linewidth, dpi, 1.4))
    plot_boundary(draw, boundary, transform, dpi)
    save_image(image, output_path)


def plot_clusters(clusters: gpd.GeoDataFrame, connectors: gpd.GeoDataFrame, boundary: gpd.GeoDataFrame, output_path, linewidth: float, dpi: int) -> None:
    size_px = int(12 * dpi)
    image, draw, transform = make_canvas(gdf_bounds(boundary), size_px)
    if not connectors.empty:
        draw_lines(draw, connectors.geometry, transform, rgba("#c7c7c7", 180), line_width_px(linewidth, dpi, 0.6))

    cluster_ids = sorted(clusters["cluster_id"].dropna().unique().tolist())
    color_map = make_cluster_color_map(cluster_ids)
    for cluster_id, group in clusters.groupby("cluster_id"):
        draw_lines(draw, group.geometry, transform, rgba(color_map[cluster_id]), line_width_px(linewidth, dpi))

    plot_boundary(draw, boundary, transform, dpi)
    save_image(image, output_path)


def plot_zoom(clusters: gpd.GeoDataFrame, connectors: gpd.GeoDataFrame, graph, boundary: gpd.GeoDataFrame, output_path, linewidth: float, dpi: int, bounds: dict[str, float]) -> None:
    ordinary_zoom = clusters.cx[bounds["west"]:bounds["east"], bounds["south"]:bounds["north"]].copy()
    connector_zoom = connectors.cx[bounds["west"]:bounds["east"], bounds["south"]:bounds["north"]].copy()
    boundary_zoom = boundary.cx[bounds["west"]:bounds["east"], bounds["south"]:bounds["north"]].copy()

    size_px = int(10 * dpi)
    image, draw, transform = make_canvas(bounds_dict_to_tuple(bounds), size_px)
    draw_lines(draw, ordinary_zoom.geometry, transform, rgba("#4a4a4a"), line_width_px(linewidth, dpi))
    draw_lines(draw, connector_zoom.geometry, transform, rgba("#e66101", 230), line_width_px(linewidth, dpi, 1.2))

    connector_width = line_width_px(0.5, dpi)
    for index, (seg_a, seg_b, attrs) in enumerate(graph.edges(data=True)):
        if index >= 250 or not attrs.get("has_connector"):
            continue
        row_a = ordinary_zoom.loc[ordinary_zoom["seg_id"] == seg_a]
        row_b = ordinary_zoom.loc[ordinary_zoom["seg_id"] == seg_b]
        if row_a.empty or row_b.empty:
            continue
        point_a = row_a.geometry.iloc[0].centroid
        point_b = row_b.geometry.iloc[0].centroid
        draw.line([transform(point_a.x, point_a.y), transform(point_b.x, point_b.y)], fill=rgba("#2b8cbe", 130), width=connector_width)

    if not boundary_zoom.empty:
        plot_boundary(draw, boundary_zoom, transform, dpi)
    save_image(image, output_path)


def main() -> None:
    ensure_directories()
    config = load_config()
    variant = sys.argv[1] if len(sys.argv) > 1 else config.get("evaluation", {}).get("default_variant", "road_only")
    algorithm = sys.argv[2] if len(sys.argv) > 2 else "louvain"
    if variant not in config["semantic_graph"]["variants"]:
        raise ValueError(f"Unknown graph variant '{variant}'. Expected one of {list(config['semantic_graph']['variants'])}.")
    algorithms = config["clustering"].get("algorithms", [config["clustering"].get("method", "louvain")])
    if algorithm not in algorithms:
        raise ValueError(f"Unknown clustering algorithm '{algorithm}'. Expected one of {algorithms}.")

    linewidth = float(config["visualization"]["linewidth"])
    dpi = int(config["visualization"]["figure_dpi"])

    classified_path = DATA_INTERIM / "road_edges_classified.gpkg"
    clusters_path = DATA_PROCESSED / f"segment_clusters_{variant}_{algorithm}.gpkg"
    graph_path = OUTPUTS_GRAPHS / f"segment_relation_graph_{variant}.gpickle"
    boundary_path = DATA_RAW / "beijing_fifth_ring_boundary.gpkg"

    classified = gpd.read_file(classified_path)
    clusters = gpd.read_file(clusters_path)
    boundary = gpd.read_file(boundary_path)
    with graph_path.open("rb") as handle:
        graph = pickle.load(handle)

    connectors = classified.loc[classified["segment_role"] == "connector"].copy()

    classification_output = OUTPUTS_FIGURES / "01_ordinary_vs_connector_segments.png"
    clusters_output = OUTPUTS_FIGURES / f"02_segment_clusters_{variant}_{algorithm}.png"
    zoom_output = OUTPUTS_FIGURES / f"03_connector_compression_zoom_{variant}_{algorithm}.png"

    plot_classification(
        classified,
        boundary,
        classification_output,
        linewidth,
        dpi,
    )
    print(f"Saved classification map to {classification_output}")
    plot_clusters(
        clusters,
        connectors,
        boundary,
        clusters_output,
        linewidth,
        dpi,
    )
    if variant == config.get("evaluation", {}).get("default_variant", "road_only") and algorithm == "louvain":
        shutil.copyfile(clusters_output, OUTPUTS_FIGURES / "02_segment_clusters_louvain.png")
    print(f"Saved cluster map to {clusters_output}")
    projected_bounds = project_bounds(
        config["visualization"]["zoom_bounds"],
        config["crs"]["geographic"],
        str(clusters.crs),
    )

    plot_zoom(
        clusters,
        connectors,
        graph,
        boundary,
        zoom_output,
        linewidth,
        dpi,
        projected_bounds,
    )
    if variant == config.get("evaluation", {}).get("default_variant", "road_only") and algorithm == "louvain":
        shutil.copyfile(zoom_output, OUTPUTS_FIGURES / "03_connector_compression_zoom.png")
    print(f"Saved connector-compression zoom to {zoom_output}")

    print(f"Saved figures to {OUTPUTS_FIGURES}")


if __name__ == "__main__":
    main()
