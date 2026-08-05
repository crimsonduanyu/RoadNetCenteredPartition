"""Mechanical recovery of the paper partition maps from commit d2139c6."""
from __future__ import annotations

import colorsys
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.transforms import blended_transform_factory
import networkx as nx
import numpy as np
import pandas as pd
from PIL import Image, ImageDraw
from shapely.geometry import LineString, MultiLineString

from roadnet_partition.io.safe_graph import read_safe_graph
from roadnet_partition.zoning.contracts import partition_mapping
from roadnet_partition.zoning.metrics import cluster_mean_origin_orders_per_slot


FALLBACK_COLORS = [
    "#0066cc", "#cc3311", "#009988", "#ee7733", "#0077bb", "#cc0077", "#33bb44", "#aa4499",
    "#ddaa33", "#004488", "#bb5566", "#228833", "#661100", "#3366aa", "#aa3377", "#447711",
]
BLUE_MONO_COLORS = [
    "#071d36", "#0a2a4f", "#0b376c", "#0c4a8a", "#0f5fa8", "#1976c2", "#2f8bd4", "#55a1df",
    "#7bb7e8", "#a5cdef", "#c4dff4", "#dbeaf7", "#132f4c", "#1f4569", "#2f5d82", "#477696",
    "#638eaa", "#83a8bf", "#a6bfce", "#c7d5de", "#111111", "#252a2e", "#3b444c", "#58636d",
    "#758390", "#95a3ad", "#b4c0c9", "#d0d9df", "#05152a", "#082243", "#0a315f", "#0b417b",
    "#135594", "#236baa", "#3d82bd", "#5a99cd", "#7aafd9", "#9cc5e4", "#bad7eb", "#d4e6f0",
    "#162435", "#24384c", "#354e64", "#4b667c", "#647f93", "#8199aa", "#9fb4c1", "#bdccd4",
]


def load_graph(path: Path) -> nx.Graph:
    graph = read_safe_graph(path)
    if any(not isinstance(node, str) for node in graph.nodes):
        graph = nx.relabel_nodes(graph, {node: str(node) for node in graph.nodes})
    return graph


def iter_line_coords(geometry: Any) -> Iterable[list[tuple[float, ...]]]:
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, LineString):
        yield list(geometry.coords)
    elif isinstance(geometry, MultiLineString):
        for line in geometry.geoms:
            yield list(line.coords)
    elif hasattr(geometry, "boundary"):
        yield from iter_line_coords(geometry.boundary)


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[:2], 16), int(value[2:4], 16), int(value[4:], 16)


def _hex(color: tuple[int, int, int]) -> str:
    return f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"


def _distance(left: tuple[int, int, int] | str, right: tuple[int, int, int] | str) -> float:
    a = _rgb(left) if isinstance(left, str) else left
    b = _rgb(right) if isinstance(right, str) else right
    return float(sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5)


def _palette() -> list[str]:
    candidates = []
    for hue_index in range(360):
        for saturation, value in ((0.92, 0.72), (0.82, 0.60), (0.72, 0.78), (0.95, 0.52)):
            color = tuple(round(channel * 255) for channel in colorsys.hsv_to_rgb(hue_index / 360, saturation, value))
            luminance = 0.2126 * color[0] / 255 + 0.7152 * color[1] / 255 + 0.0722 * color[2] / 255
            if 0.12 <= luminance <= 0.68:
                candidates.append(color)
    selected = [_rgb(color) for color in FALLBACK_COLORS]
    remaining = [color for color in candidates if all(_distance(color, used) >= 18 for used in selected)]
    while remaining and len(selected) < 220:
        next_color = max(
            remaining,
            key=lambda color: (
                min(_distance(color, used) for used in selected),
                np.mean([_distance(color, used) for used in selected]),
            ),
        )
        selected.append(next_color)
        remaining = [color for color in remaining if _distance(color, next_color) >= 18]
    return [_hex(color) for color in selected]


def muted_palette(size: int = 220) -> list[str]:
    """Low-saturation categorical colors, adjacency ordering supplies local contrast."""
    colors = []
    lightness = (0.48, 0.62, 0.40, 0.70)
    for index in range(size):
        red, green, blue = colorsys.hls_to_rgb((index * 0.61803398875) % 1.0, lightness[index % 4], 0.36)
        colors.append(_hex((round(red * 255), round(green * 255), round(blue * 255))))
    return colors


def cluster_colors(clusters: gpd.GeoDataFrame, graph: nx.Graph, palette: list[str] | None = None) -> dict[Any, str]:
    partition = partition_mapping(clusters)
    adjacency: dict[Any, set[Any]] = {cluster_id: set() for cluster_id in set(partition.values())}
    for node_a, node_b in graph.edges:
        cluster_a, cluster_b = partition.get(str(node_a)), partition.get(str(node_b))
        if cluster_a is None or cluster_b is None or cluster_a == cluster_b:
            continue
        adjacency[cluster_a].add(cluster_b)
        adjacency[cluster_b].add(cluster_a)
    assigned: dict[Any, str] = {}
    colors = palette or _palette()
    order = sorted(adjacency, key=lambda cluster_id: (len(adjacency[cluster_id]), str(cluster_id)), reverse=True)
    for cluster_id in order:
        neighbor_colors = [assigned[n] for n in adjacency[cluster_id] if n in assigned]
        unused = [color for color in colors if color not in assigned.values()] or colors
        assigned[cluster_id] = max(
            unused,
            key=lambda color: (
                min((_distance(color, other) for other in neighbor_colors), default=255),
                np.mean([_distance(color, other) for other in assigned.values()]) if assigned else 255,
            ),
        )
    return assigned


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    return (*_rgb(color), alpha)


def _draw_lines(draw: ImageDraw.ImageDraw, geometries: Iterable[Any], transform: Any, color: Any, width: int) -> None:
    for geometry in geometries:
        for coords in iter_line_coords(geometry):
            if len(coords) >= 2:
                draw.line([transform(x, y) for x, y, *_ in coords], fill=color, width=width)


def _line_width(points: float, dpi: int, multiplier: float = 1.0) -> int:
    return max(1, round(points * multiplier * dpi / 72))


def plot_partition_png(
    clusters: gpd.GeoDataFrame,
    connectors: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    graph: nx.Graph,
    output: Path,
    palette: list[str] | None = None,
    halo_color: str = "#ffffff",
    size_px: int = 1800,
    dpi: int = 300,
    linewidth: float = 0.45,
) -> None:
    minx, miny, maxx, maxy = map(float, boundary.total_bounds)
    padding = round(size_px * 0.025)
    drawable = size_px - 2 * padding
    scale = min(drawable / (maxx - minx), drawable / (maxy - miny))
    offset_x = padding + (drawable - (maxx - minx) * scale) / 2
    offset_y = padding + (drawable - (maxy - miny) * scale) / 2
    transform = lambda x, y: (offset_x + (x - minx) * scale, size_px - offset_y - (y - miny) * scale)
    image = Image.new("RGBA", (size_px, size_px), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_lines(draw, connectors.geometry, transform, _rgba("#bdbdbd", 70), _line_width(linewidth, dpi, 0.45))
    colors = cluster_colors(clusters, graph, palette)
    for _, group in clusters.groupby("cluster_id"):
        _draw_lines(draw, group.geometry, transform, _rgba(halo_color, 235), _line_width(linewidth, dpi, 2.5))
    for cluster_id, group in clusters.groupby("cluster_id"):
        _draw_lines(draw, group.geometry, transform, _rgba(colors[cluster_id]), _line_width(linewidth, dpi))
    _draw_lines(draw, boundary.geometry, transform, _rgba("#111111"), _line_width(1.2, dpi))
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)


def plot_partition_pdf(
    clusters: gpd.GeoDataFrame,
    connectors: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    graph: nx.Graph,
    output: Path,
    palette: list[str] | None = None,
    halo_color: str = "#ffffff",
    size_px: int = 1800,
    dpi: int = 300,
    linewidth: float = 0.45,
) -> None:
    minx, miny, maxx, maxy = map(float, boundary.total_bounds)
    center_x, center_y = (minx + maxx) / 2, (miny + maxy) / 2
    view_span = max(maxx - minx, maxy - miny) / 0.95
    fig, ax = plt.subplots(figsize=(size_px / dpi, size_px / dpi))
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")

    def lines(geometries: Iterable[Any], color: str, width: float, alpha: float, zorder: int) -> None:
        for geometry in geometries:
            for coords in iter_line_coords(geometry):
                if len(coords) >= 2:
                    ax.plot(
                        [point[0] for point in coords], [point[1] for point in coords],
                        color=color, linewidth=width, alpha=alpha, zorder=zorder,
                    )

    lines(connectors.geometry, "#bdbdbd", linewidth * 0.45, 70 / 255, 1)
    colors = cluster_colors(clusters, graph, palette)
    for _, group in clusters.groupby("cluster_id"):
        lines(group.geometry, halo_color, linewidth * 2.5, 0.92, 2)
    for cluster_id, group in clusters.groupby("cluster_id"):
        lines(group.geometry, colors[cluster_id], linewidth, 1, 3)
    lines(boundary.geometry, "#111111", 1.2, 1, 4)
    ax.set_xlim(center_x - view_span / 2, center_x + view_span / 2)
    ax.set_ylim(center_y - view_span / 2, center_y + view_span / 2)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    fig.savefig(output, format="pdf", transparent=True, facecolor="none", edgecolor="none")
    plt.close(fig)


def render_partition_maps(
    partition_path: Path,
    classified_edges_path: Path,
    boundary: gpd.GeoDataFrame,
    graph_path: Path,
    output_dir: Path,
) -> None:
    """Render the partition maps against an already validated ``boundary``.

    The boundary arrives as a GeoDataFrame rather than a path because it must
    have satisfied ``BoundaryArtifactV1`` before any output can be created; see
    ``roadnet_partition.reporting.boundary_resolver``.
    """
    clusters = gpd.read_file(partition_path)
    classified = gpd.read_file(classified_edges_path)
    connectors = classified.loc[classified["segment_role"] == "connector"].copy()
    graph = load_graph(graph_path)
    for stem, palette, halo in (
        ("best_partition_map", None, "#ffffff"),
        ("best_partition_map_blue", BLUE_MONO_COLORS, "#d8e2ee"),
    ):
        plot_partition_png(clusters, connectors, boundary, graph, output_dir / f"{stem}.png", palette, halo)
        plot_partition_pdf(clusters, connectors, boundary, graph, output_dir / f"{stem}.pdf", palette, halo)


def _scale_length_data(crs: Any, target_meters: float = 5000.0) -> float:
    """Scale-bar length in data units for a real-world ``target_meters``."""
    if crs is not None:
        try:
            if crs.is_geographic:
                return target_meters / 111_320.0
        except Exception:
            return target_meters
    return target_meters


def _panel_label(ax: Any, text: str) -> None:
    ax.text(
        0.02, 0.97, text, transform=ax.transAxes,
        fontsize=11, fontweight="bold", va="top", ha="left",
        zorder=8, clip_on=False,
    )


def _north_arrow(ax: Any, loc: tuple[float, float] = (0.93, 0.90), length: float = 0.05) -> None:
    x, y = loc
    ax.annotate(
        "", xy=(x, y), xytext=(x, y - length), xycoords=ax.transAxes,
        arrowprops=dict(arrowstyle="-|>,head_width=0.35,head_length=0.7", color="black", lw=1.3),
        zorder=8, clip_on=False,
    )
    ax.text(
        x, y + 0.012, "N", transform=ax.transAxes,
        ha="center", va="bottom", fontsize=9, fontweight="bold", zorder=8, clip_on=False,
    )


def _scale_bar(ax: Any, length: float, label: str = "5 km", loc: tuple[float, float] = (0.05, 0.055)) -> None:
    trans = blended_transform_factory(ax.transData, ax.transAxes)
    xlim = ax.get_xlim()
    x0 = float(xlim[0]) + loc[0] * (float(xlim[1]) - float(xlim[0]))
    y0 = loc[1]
    tick = 0.010
    ax.plot([x0, x0 + length], [y0, y0], transform=trans, color="black", linewidth=2.4, zorder=8, clip_on=False)
    ax.plot([x0, x0], [y0 - tick, y0 + tick], transform=trans, color="black", linewidth=1.5, zorder=8, clip_on=False)
    ax.plot([x0 + length, x0 + length], [y0 - tick, y0 + tick], transform=trans, color="black", linewidth=1.5, zorder=8, clip_on=False)
    ax.text(
        x0 + length / 2, y0 + tick + 0.006, label, transform=trans,
        ha="center", va="bottom", fontsize=8, zorder=8, clip_on=False,
    )


def render_partition_order_figure(
    partition_path: Path,
    classified_edges_path: Path,
    boundary: gpd.GeoDataFrame,
    graph_path: Path,
    hourly_od_path: Path,
    png_path: Path,
    pdf_path: Path,
    dpi: int = 300,
) -> None:
    """Render the recovered two-panel partition and mean-hourly-order figure.

    ``boundary`` is an already validated GeoDataFrame; see
    ``roadnet_partition.reporting.boundary_resolver``.
    """
    clusters = gpd.read_file(partition_path)
    classified = gpd.read_file(classified_edges_path)
    connectors = classified.loc[classified["segment_role"] == "connector"].copy()
    graph = load_graph(graph_path)
    partition = partition_mapping(clusters)
    hourly_od = pd.read_csv(hourly_od_path, parse_dates=["slot_start"])
    mean_orders = cluster_mean_origin_orders_per_slot(hourly_od, partition)
    categorical = cluster_colors(clusters, graph, muted_palette())

    minx, miny, maxx, maxy = map(float, boundary.total_bounds)
    center_x, center_y = (minx + maxx) / 2, (miny + maxy) / 2
    view_span = max(maxx - minx, maxy - miny) / 0.95
    scale_length = _scale_length_data(clusters.crs)
    norm = Normalize(vmin=float(mean_orders.min()), vmax=float(mean_orders.max()))
    cmap = LinearSegmentedColormap.from_list(
        "mean_orders_gray_blue",
        ["#aeb4ba", "#9fabb4", "#839eb2", "#5f8eae", "#3276a6", "#07579a", "#003b73"],
    )
    fig, axes = plt.subplots(1, 2, figsize=(12.4, 6.2))

    def lines(ax: Any, geometries: Iterable[Any], color: Any, width: float, alpha: float, zorder: int) -> None:
        for geometry in geometries:
            for coords in iter_line_coords(geometry):
                if len(coords) >= 2:
                    ax.plot(
                        [point[0] for point in coords], [point[1] for point in coords],
                        color=color, linewidth=width, alpha=alpha, zorder=zorder,
                    )

    for ax in axes:
        lines(ax, connectors.geometry, "#c7c7c7", 0.20, 0.45, 1)
        for _, group in clusters.groupby("cluster_id"):
            lines(ax, group.geometry, "#ffffff", 0.95, 0.90, 2)
        lines(ax, boundary.geometry, "#202020", 1.0, 1.0, 4)
        ax.set_xlim(center_x - view_span / 2, center_x + view_span / 2)
        ax.set_ylim(center_y - view_span / 2, center_y + view_span / 2)
        ax.set_aspect("equal", adjustable="box")
        ax.axis("off")

    for cluster_id, group in clusters.groupby("cluster_id"):
        lines(axes[0], group.geometry, categorical[cluster_id], 0.42, 1.0, 3)
        lines(axes[1], group.geometry, cmap(norm(float(mean_orders.loc[cluster_id]))), 0.52, 1.0, 3)

    colorbar = fig.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=axes[1], fraction=0.04, pad=0.02)
    colorbar.set_label("Mean orders per hour", fontsize=10)
    colorbar.ax.tick_params(labelsize=8)
    for ax, label in zip(axes, ("(a)", "(b)")):
        _panel_label(ax, label)
        _north_arrow(ax)
    _scale_bar(axes[0], scale_length)
    fig.subplots_adjust(left=0.01, right=0.965, bottom=0.015, top=0.985, wspace=0.04)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path, format="png", dpi=dpi, facecolor="white", bbox_inches="tight", pad_inches=0.03)
    fig.savefig(pdf_path, format="pdf", facecolor="white", bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)
