from __future__ import annotations

import colorsys
from dataclasses import dataclass
from pathlib import Path
import pickle
import sys
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from PIL import Image, ImageDraw
import yaml
from shapely.geometry import LineString, MultiLineString


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from utils_geo import project_bounds  # noqa: E402


FALLBACK_COLORS = [
    "#0066cc",
    "#cc3311",
    "#009988",
    "#ee7733",
    "#0077bb",
    "#cc0077",
    "#33bb44",
    "#aa4499",
    "#ddaa33",
    "#004488",
    "#bb5566",
    "#228833",
    "#661100",
    "#3366aa",
    "#aa3377",
    "#447711",
]


@dataclass(frozen=True)
class BestSelection:
    run_id: str
    algorithm: str
    initialization: str
    setting_id: str
    clusters_gpkg: Path
    balanced_score: float
    row: pd.Series


def project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Missing {label}: {path}")


def load_graph(path: Path) -> nx.Graph:
    with path.open("rb") as handle:
        graph = pickle.load(handle)
    if any(not isinstance(node, str) for node in graph.nodes):
        graph = nx.relabel_nodes(graph, {node: str(node) for node in graph.nodes})
    return graph


def resolve_run_paths(config: dict[str, Any]) -> tuple[Path, Path, Path]:
    run_root = project_path(config["outputs"]["root"])
    tables_dir = run_root / "tables"
    figures_dir = project_path(config["visualization"]["output_dir"])
    figures_dir.mkdir(parents=True, exist_ok=True)
    return run_root, tables_dir, figures_dir


def validate_inputs(config: dict[str, Any], tables_dir: Path) -> None:
    input_labels = {
        "graph": "relation graph",
        "classified_edges": "classified road edges",
        "boundary": "study boundary",
    }
    for key, label in input_labels.items():
        require_file(project_path(config["inputs"][key]), label)

    for name, path_value in config["inputs"]["baseline_clusters"].items():
        require_file(project_path(path_value), f"baseline cluster file for {name}")

    for name in ["metrics_regularized.csv", "run_manifest.csv", "objective_trace.csv"]:
        require_file(tables_dir / name, name)


def add_balanced_score(metrics: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    result = metrics.copy()
    weights = config["visualization"]["best_selection"]["metrics"]
    regularized = result["source_type"] == "regularized"
    for metric, weight in weights.items():
        if metric not in result.columns:
            raise ValueError(f"Best-selection metric is missing from metrics_regularized.csv: {metric}")
        values = pd.to_numeric(result.loc[regularized, metric], errors="coerce")
        lo = float(values.min())
        hi = float(values.max())
        normalized_column = f"{metric}_normalized"
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            result[normalized_column] = 0.0
        else:
            result[normalized_column] = (pd.to_numeric(result[metric], errors="coerce") - lo) / (hi - lo)
        result.loc[~regularized, normalized_column] = np.nan

    score = pd.Series(0.0, index=result.index, dtype=float)
    for metric, weight in weights.items():
        score += float(weight) * result[f"{metric}_normalized"].fillna(0.0)
    result["balanced_score"] = score
    result.loc[~regularized, "balanced_score"] = np.nan
    return result


def select_best_run(metrics: pd.DataFrame, manifest: pd.DataFrame, config: dict[str, Any]) -> BestSelection:
    candidates = metrics.loc[metrics["source_type"] == "regularized"].copy()
    if bool(config["visualization"]["best_selection"].get("require_connected", True)):
        candidates = candidates.loc[pd.to_numeric(candidates["connected_cluster_ratio"], errors="coerce") >= 1.0]
    candidates = candidates.dropna(subset=["balanced_score"]).copy()
    if candidates.empty:
        raise RuntimeError("No connected regularized candidate is available for visualization.")

    best_row = candidates.sort_values(["balanced_score", "run_id"]).iloc[0]
    manifest_match = manifest.loc[
        (manifest["algorithm"] == best_row["algorithm"])
        & (manifest["setting_id"] == best_row["setting_id"])
    ]
    if manifest_match.empty:
        raise RuntimeError(f"Unable to locate selected run in run_manifest.csv: {best_row['run_id']}")
    manifest_row = manifest_match.iloc[0]
    clusters_gpkg = project_path(manifest_row["clusters_gpkg"])
    require_file(clusters_gpkg, f"selected cluster file for {best_row['run_id']}")
    return BestSelection(
        run_id=str(best_row["run_id"]),
        algorithm=str(best_row["algorithm"]),
        initialization=str(best_row["initialization"]),
        setting_id=str(best_row["setting_id"]),
        clusters_gpkg=clusters_gpkg,
        balanced_score=float(best_row["balanced_score"]),
        row=best_row,
    )


def write_best_summary(best: BestSelection, metrics: pd.DataFrame, output_path: Path) -> None:
    columns = [
        "run_id",
        "algorithm",
        "initialization",
        "setting_id",
        "balanced_score",
        "num_clusters",
        "connected_cluster_ratio",
        "od_sparsity",
        "connector_edge_cut_ratio",
        "continuity_edge_cut_ratio",
        "order_count_cv",
        "capacity_violation_ratio",
        "historical_avg_wape",
        "mean_network_diameter_m",
        "mean_elongation",
    ]
    row = best.row.copy()
    row["balanced_score"] = best.balanced_score
    summary = pd.DataFrame([{column: row.get(column, np.nan) for column in columns}])
    summary["clusters_gpkg"] = str(best.clusters_gpkg.relative_to(PROJECT_ROOT))
    summary.to_csv(output_path, index=False)

    scored = metrics.loc[metrics["source_type"] == "regularized"].copy()
    scored = scored.sort_values(["balanced_score", "run_id"])
    scored[["run_id", "algorithm", "initialization", "setting_id", "balanced_score"]].to_csv(
        output_path.with_name("balanced_score_ranking.csv"),
        index=False,
    )


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


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    r, g, b, _ = rgba(hex_color)
    return r, g, b


def rgb_to_hex(color: tuple[int, int, int]) -> str:
    return f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"


def relative_luminance(color: tuple[int, int, int]) -> float:
    r, g, b = [value / 255.0 for value in color]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def rgb_distance(color_a: tuple[int, int, int] | str, color_b: tuple[int, int, int] | str) -> float:
    if isinstance(color_a, str):
        color_a = hex_to_rgb(color_a)
    if isinstance(color_b, str):
        color_b = hex_to_rgb(color_b)
    return float(sum((float(a) - float(b)) ** 2 for a, b in zip(color_a, color_b)) ** 0.5)


def generate_high_contrast_palette() -> list[str]:
    candidates: list[tuple[int, int, int]] = []
    for hue_index in range(360):
        hue = hue_index / 360.0
        for saturation, value in [(0.92, 0.72), (0.82, 0.60), (0.72, 0.78), (0.95, 0.52)]:
            red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
            color = (int(round(red * 255)), int(round(green * 255)), int(round(blue * 255)))
            luminance = relative_luminance(color)
            if 0.12 <= luminance <= 0.68:
                candidates.append(color)

    selected = [hex_to_rgb(color) for color in FALLBACK_COLORS]
    remaining = [
        color
        for color in candidates
        if all(rgb_distance(color, existing) >= 18.0 for existing in selected)
    ]
    while remaining and len(selected) < 220:
        next_color = max(
            remaining,
            key=lambda color: (
                min(rgb_distance(color, existing) for existing in selected),
                np.mean([rgb_distance(color, existing) for existing in selected]),
            ),
        )
        selected.append(next_color)
        remaining = [color for color in remaining if rgb_distance(color, next_color) >= 18.0]
    return [rgb_to_hex(color) for color in selected]


def partition_from_clusters(clusters: gpd.GeoDataFrame) -> dict[str, Any]:
    return dict(zip(clusters["seg_id"].astype(str), clusters["cluster_id"]))


def build_cluster_adjacency(graph: nx.Graph, partition: dict[str, Any]) -> dict[Any, set[Any]]:
    adjacency: dict[Any, set[Any]] = {cluster_id: set() for cluster_id in set(partition.values())}
    for node_a, node_b in graph.edges:
        cluster_a = partition.get(str(node_a))
        cluster_b = partition.get(str(node_b))
        if cluster_a is None or cluster_b is None or cluster_a == cluster_b:
            continue
        adjacency.setdefault(cluster_a, set()).add(cluster_b)
        adjacency.setdefault(cluster_b, set()).add(cluster_a)
    return adjacency


def make_adjacency_contrast_color_map(
    cluster_ids: list[Any],
    cluster_adjacency: dict[Any, set[Any]],
) -> dict[Any, str]:
    palette = generate_high_contrast_palette()
    if not palette:
        palette = FALLBACK_COLORS
    assigned: dict[Any, str] = {}
    ordered_clusters = sorted(
        cluster_ids,
        key=lambda cluster_id: (len(cluster_adjacency.get(cluster_id, set())), str(cluster_id)),
        reverse=True,
    )

    for cluster_id in ordered_clusters:
        neighbor_colors = [
            assigned[neighbor]
            for neighbor in cluster_adjacency.get(cluster_id, set())
            if neighbor in assigned
        ]
        used_colors = set(assigned.values())
        available = [color for color in palette if color not in used_colors]
        candidates = available or palette

        def candidate_score(color: str) -> tuple[float, float]:
            if neighbor_colors:
                min_neighbor_distance = min(rgb_distance(color, neighbor_color) for neighbor_color in neighbor_colors)
            else:
                min_neighbor_distance = 255.0
            if assigned:
                mean_global_distance = float(np.mean([rgb_distance(color, other) for other in assigned.values()]))
            else:
                mean_global_distance = 255.0
            return min_neighbor_distance, mean_global_distance

        assigned[cluster_id] = max(candidates, key=candidate_score)
    return assigned


def cluster_color_diagnostics(
    cluster_ids: list[Any],
    color_map: dict[Any, str],
    cluster_adjacency: dict[Any, set[Any]],
) -> pd.DataFrame:
    rows = []
    for cluster_id in cluster_ids:
        neighbor_distances = [
            rgb_distance(color_map[cluster_id], color_map[neighbor])
            for neighbor in cluster_adjacency.get(cluster_id, set())
            if neighbor in color_map
        ]
        rows.append(
            {
                "cluster_id": cluster_id,
                "color": color_map.get(cluster_id),
                "degree": len(cluster_adjacency.get(cluster_id, set())),
                "min_adjacent_color_distance": min(neighbor_distances) if neighbor_distances else np.nan,
                "mean_adjacent_color_distance": float(np.mean(neighbor_distances)) if neighbor_distances else np.nan,
            }
        )
    return pd.DataFrame(rows).sort_values(["min_adjacent_color_distance", "degree"], ascending=[True, False])


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


def gdf_bounds(gdf: gpd.GeoDataFrame) -> tuple[float, float, float, float]:
    return tuple(float(value) for value in gdf.total_bounds)


def bounds_dict_to_tuple(bounds: dict[str, float]) -> tuple[float, float, float, float]:
    return bounds["west"], bounds["south"], bounds["east"], bounds["north"]


def make_cluster_color_map(
    cluster_ids: list[Any],
    clusters: gpd.GeoDataFrame,
    graph: nx.Graph,
    config: dict[str, Any],
) -> tuple[dict[Any, str], dict[Any, set[Any]]]:
    partition = partition_from_clusters(clusters)
    adjacency = build_cluster_adjacency(graph, partition)
    strategy = config["visualization"].get("cluster_palette_strategy", "adjacency_contrast")
    if strategy == "adjacency_contrast":
        return make_adjacency_contrast_color_map(cluster_ids, adjacency), adjacency
    fallback = {
        cluster_id: FALLBACK_COLORS[index % len(FALLBACK_COLORS)]
        for index, cluster_id in enumerate(cluster_ids)
    }
    return fallback, adjacency


def draw_cluster_lines(
    draw: ImageDraw.ImageDraw,
    clusters: gpd.GeoDataFrame,
    transform,
    color_map: dict[Any, str],
    linewidth: float,
    dpi: int,
    config: dict[str, Any],
    multiplier: float = 1.0,
) -> None:
    if bool(config["visualization"].get("cluster_line_halo", True)):
        halo_multiplier = float(config["visualization"].get("cluster_line_halo_multiplier", 2.5)) * multiplier
        for _, group in clusters.groupby("cluster_id"):
            draw_lines(draw, group.geometry, transform, rgba("#ffffff", 235), line_width_px(linewidth, dpi, halo_multiplier))
    for cluster_id, group in clusters.groupby("cluster_id"):
        draw_lines(draw, group.geometry, transform, rgba(color_map[cluster_id]), line_width_px(linewidth, dpi, multiplier))


def plot_cluster_map(
    clusters: gpd.GeoDataFrame,
    connectors: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    graph: nx.Graph,
    config: dict[str, Any],
    output_path: Path,
    size_px: int,
    linewidth: float,
    dpi: int,
    title: str | None = None,
) -> Image.Image:
    image, draw, transform = make_canvas(gdf_bounds(boundary), size_px)
    if not connectors.empty:
        connector_alpha = int(config["visualization"].get("connector_alpha", 70))
        draw_lines(draw, connectors.geometry, transform, rgba("#bdbdbd", connector_alpha), line_width_px(linewidth, dpi, 0.45))
    cluster_ids = sorted(clusters["cluster_id"].dropna().unique().tolist(), key=lambda value: str(value))
    color_map, _ = make_cluster_color_map(cluster_ids, clusters, graph, config)
    draw_cluster_lines(draw, clusters, transform, color_map, linewidth, dpi, config)
    draw_lines(draw, boundary.geometry, transform, rgba("#111111"), line_width_px(1.2, dpi))
    if title:
        draw.rectangle((0, 0, size_px, int(size_px * 0.055)), fill=(255, 255, 255, 230))
        draw.text((int(size_px * 0.025), int(size_px * 0.017)), title, fill=(20, 20, 20, 255))
    image.convert("RGB").save(output_path)
    return image


def plot_zoom_map(
    clusters: gpd.GeoDataFrame,
    connectors: gpd.GeoDataFrame,
    graph: nx.Graph,
    boundary: gpd.GeoDataFrame,
    config: dict[str, Any],
    output_path: Path,
    bounds: dict[str, float],
    size_px: int,
    linewidth: float,
    dpi: int,
) -> None:
    ordinary_zoom = clusters.cx[bounds["west"]:bounds["east"], bounds["south"]:bounds["north"]].copy()
    connector_zoom = connectors.cx[bounds["west"]:bounds["east"], bounds["south"]:bounds["north"]].copy()
    boundary_zoom = boundary.cx[bounds["west"]:bounds["east"], bounds["south"]:bounds["north"]].copy()

    image, draw, transform = make_canvas(bounds_dict_to_tuple(bounds), size_px)
    if not connector_zoom.empty:
        connector_alpha = int(config["visualization"].get("connector_alpha", 70))
        draw_lines(draw, connector_zoom.geometry, transform, rgba("#969696", connector_alpha), line_width_px(linewidth, dpi, 0.55))

    cluster_ids = sorted(clusters["cluster_id"].dropna().unique().tolist(), key=lambda value: str(value))
    color_map, _ = make_cluster_color_map(cluster_ids, clusters, graph, config)
    draw_cluster_lines(draw, ordinary_zoom, transform, color_map, linewidth, dpi, config, multiplier=1.2)

    connector_width = line_width_px(0.55, dpi)
    by_id = ordinary_zoom.set_index("seg_id")
    for seg_a, seg_b, attrs in graph.edges(data=True):
        if not attrs.get("has_connector"):
            continue
        if seg_a not in by_id.index or seg_b not in by_id.index:
            continue
        point_a = by_id.loc[seg_a].geometry.centroid
        point_b = by_id.loc[seg_b].geometry.centroid
        draw.line([transform(point_a.x, point_a.y), transform(point_b.x, point_b.y)], fill=rgba("#2b8cbe", 85), width=connector_width)

    if not boundary_zoom.empty:
        draw_lines(draw, boundary_zoom.geometry, transform, rgba("#111111"), line_width_px(1.2, dpi))
    image.convert("RGB").save(output_path)


def plot_comparison_maps(
    panels: list[tuple[str, Path]],
    connectors: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    graph: nx.Graph,
    config: dict[str, Any],
    output_path: Path,
    size_px: int,
    linewidth: float,
    dpi: int,
) -> None:
    panel_images = []
    for title, clusters_path in panels:
        clusters = gpd.read_file(clusters_path)
        tmp_path = output_path.with_name(f"_{title.replace(' ', '_').lower()}_tmp.png")
        image = plot_cluster_map(clusters, connectors, boundary, graph, config, tmp_path, size_px, linewidth, dpi, title)
        panel_images.append(image.convert("RGB"))
        tmp_path.unlink(missing_ok=True)
    width = sum(image.width for image in panel_images)
    height = max(image.height for image in panel_images)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    offset = 0
    for image in panel_images:
        canvas.paste(image, (offset, 0))
        offset += image.width
    canvas.save(output_path)


def pareto_frontier(frame: pd.DataFrame, x: str, y: str) -> pd.DataFrame:
    work = frame[[x, y]].apply(pd.to_numeric, errors="coerce").dropna().sort_values(x)
    rows = []
    best_y = np.inf
    for _, row in work.iterrows():
        if row[y] < best_y:
            rows.append(row)
            best_y = row[y]
    return pd.DataFrame(rows)


def text_size(draw: ImageDraw.ImageDraw, text: str) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text)
    return box[2] - box[0], box[3] - box[1]


def data_bounds(values: pd.Series) -> tuple[float, float]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return 0.0, 1.0
    lo = float(numeric.min())
    hi = float(numeric.max())
    if not np.isfinite(lo) or not np.isfinite(hi):
        return 0.0, 1.0
    if abs(hi - lo) <= 1.0e-12:
        pad = max(abs(lo) * 0.05, 1.0)
        return lo - pad, hi + pad
    pad = 0.06 * (hi - lo)
    return lo - pad, hi + pad


def chart_transform(
    x_value: float,
    y_value: float,
    x_bounds: tuple[float, float],
    y_bounds: tuple[float, float],
    plot_box: tuple[int, int, int, int],
) -> tuple[int, int]:
    left, top, right, bottom = plot_box
    x0, x1 = x_bounds
    y0, y1 = y_bounds
    px = left + (float(x_value) - x0) / (x1 - x0) * (right - left)
    py = bottom - (float(y_value) - y0) / (y1 - y0) * (bottom - top)
    return int(round(px)), int(round(py))


def draw_axes(
    draw: ImageDraw.ImageDraw,
    plot_box: tuple[int, int, int, int],
    x_label: str,
    y_label: str,
    title: str,
    x_bounds: tuple[float, float],
    y_bounds: tuple[float, float],
) -> None:
    left, top, right, bottom = plot_box
    draw.rectangle(plot_box, outline=rgba("#333333"), width=1)
    for index in range(6):
        x = left + int(round(index * (right - left) / 5))
        y = top + int(round(index * (bottom - top) / 5))
        draw.line((x, top, x, bottom), fill=rgba("#dddddd"), width=1)
        draw.line((left, y, right, y), fill=rgba("#dddddd"), width=1)
        x_value = x_bounds[0] + index * (x_bounds[1] - x_bounds[0]) / 5
        y_value = y_bounds[1] - index * (y_bounds[1] - y_bounds[0]) / 5
        draw.text((x - 18, bottom + 8), f"{x_value:.3g}", fill=rgba("#333333"))
        draw.text((left - 68, y - 6), f"{y_value:.3g}", fill=rgba("#333333"))
    title_width, _ = text_size(draw, title)
    draw.text(((left + right - title_width) // 2, 18), title, fill=rgba("#111111"))
    label_width, _ = text_size(draw, x_label)
    draw.text(((left + right - label_width) // 2, bottom + 34), x_label, fill=rgba("#111111"))
    draw.text((16, (top + bottom) // 2), y_label, fill=rgba("#111111"))


def draw_star(draw: ImageDraw.ImageDraw, center: tuple[int, int], radius: int, color: tuple[int, int, int, int]) -> None:
    cx, cy = center
    points = []
    for index in range(10):
        angle = -np.pi / 2 + index * np.pi / 5
        current = radius if index % 2 == 0 else radius * 0.42
        points.append((cx + current * np.cos(angle), cy + current * np.sin(angle)))
    draw.polygon(points, fill=color, outline=rgba("#7f0000"))


def plot_tradeoff(metrics: pd.DataFrame, best: BestSelection, pair: dict[str, str], output_path: Path, dpi: int) -> None:
    x = pair["x"]
    y = pair["y"]
    for column in [x, y]:
        if column not in metrics.columns:
            raise ValueError(f"Trade-off metric is missing: {column}")
    image = Image.new("RGB", (1500, 1100), (255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    plot_box = (130, 80, 1430, 980)
    x_bounds = data_bounds(metrics[x])
    y_bounds = data_bounds(metrics[y])
    title = pair["name"].replace("_", " ")
    draw_axes(draw, plot_box, pair.get("x_label", x), pair.get("y_label", y), title, x_bounds, y_bounds)

    baseline = metrics.loc[metrics["source_type"] == "baseline"].copy()
    regularized = metrics.loc[metrics["source_type"] == "regularized"].copy()
    for _, row in regularized.iterrows():
        px, py = chart_transform(row[x], row[y], x_bounds, y_bounds, plot_box)
        draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill=rgba("#2b8cbe", 165))
    for _, row in baseline.iterrows():
        px, py = chart_transform(row[x], row[y], x_bounds, y_bounds, plot_box)
        draw.rectangle((px - 7, py - 7, px + 7, py + 7), fill=rgba("#4d4d4d", 220))
        draw.text((px + 8, py - 12), str(row["algorithm"]), fill=rgba("#333333"))

    best_point = regularized.loc[regularized["run_id"] == best.run_id].iloc[0]
    best_px, best_py = chart_transform(best_point[x], best_point[y], x_bounds, y_bounds, plot_box)
    draw_star(draw, (best_px, best_py), 18, rgba("#d7301f", 255))

    frontier = pareto_frontier(metrics, x, y)
    if len(frontier) >= 2:
        points = [chart_transform(row[x], row[y], x_bounds, y_bounds, plot_box) for _, row in frontier.iterrows()]
        draw.line(points, fill=rgba("#756bb1", 230), width=3)
    draw.rectangle((1050, 96, 1420, 196), fill=rgba("#ffffff", 230), outline=rgba("#cccccc"))
    draw.rectangle((1070, 116, 1086, 132), fill=rgba("#4d4d4d", 220))
    draw.text((1098, 112), "Baseline", fill=rgba("#333333"))
    draw.ellipse((1069, 146, 1087, 164), fill=rgba("#2b8cbe", 165))
    draw.text((1098, 144), "Regularized grid", fill=rgba("#333333"))
    draw_star(draw, (1078, 184), 10, rgba("#d7301f", 255))
    draw.text((1098, 176), "Selected best", fill=rgba("#333333"))
    image.save(output_path)


def heat_color(value: float, lo: float, hi: float) -> tuple[int, int, int, int]:
    if not np.isfinite(value):
        return rgba("#f0f0f0")
    ratio = 0.0 if hi <= lo else (value - lo) / (hi - lo)
    ratio = min(max(float(ratio), 0.0), 1.0)
    # Low values are darker blue, high values are light orange.
    r = int(33 + ratio * (253 - 33))
    g = int(113 + ratio * (174 - 113))
    b = int(181 + ratio * (97 - 181))
    return r, g, b, 255


def draw_heatmap_panel(
    draw: ImageDraw.ImageDraw,
    table: pd.DataFrame,
    panel_box: tuple[int, int, int, int],
    title: str,
) -> None:
    left, top, right, bottom = panel_box
    label_w = 70
    title_h = 38
    x_label_h = 30
    grid_left = left + label_w
    grid_top = top + title_h
    grid_right = right - 12
    grid_bottom = bottom - x_label_h
    values = table.to_numpy(dtype=float)
    finite = values[np.isfinite(values)]
    lo = float(finite.min()) if finite.size else 0.0
    hi = float(finite.max()) if finite.size else 1.0
    rows, cols = values.shape
    cell_w = max(1, int((grid_right - grid_left) / max(cols, 1)))
    cell_h = max(1, int((grid_bottom - grid_top) / max(rows, 1)))
    draw.text((left + 6, top + 8), title, fill=rgba("#111111"))
    for row_index, row_label in enumerate(table.index):
        y0 = grid_top + row_index * cell_h
        draw.text((left + 8, y0 + cell_h // 2 - 6), str(row_label), fill=rgba("#333333"))
        for col_index, col_label in enumerate(table.columns):
            x0 = grid_left + col_index * cell_w
            value = values[row_index, col_index]
            draw.rectangle((x0, y0, x0 + cell_w - 2, y0 + cell_h - 2), fill=heat_color(value, lo, hi))
            if np.isfinite(value):
                draw.text((x0 + 6, y0 + cell_h // 2 - 6), f"{value:.3f}", fill=rgba("#ffffff"))
            if row_index == rows - 1:
                draw.text((x0 + cell_w // 2 - 10, grid_bottom + 8), str(col_label), fill=rgba("#333333"))
    draw.text((left + 8, bottom - 22), "lambda_c", fill=rgba("#333333"))
    draw.text((grid_left + (grid_right - grid_left) // 2 - 28, bottom - 22), "lambda_r", fill=rgba("#333333"))
    draw.rectangle((grid_left, grid_top, grid_left + cols * cell_w, grid_top + rows * cell_h), outline=rgba("#333333"))


def plot_heatmaps(metrics: pd.DataFrame, config: dict[str, Any], figures_dir: Path, dpi: int) -> None:
    regularized = metrics.loc[metrics["source_type"] == "regularized"].copy()
    heatmap_metrics = config["visualization"]["heatmap_metrics"]
    for initialization, group in regularized.groupby("initialization"):
        panel_w = 420
        panel_h = 330
        image = Image.new("RGB", (panel_w * len(heatmap_metrics), panel_h + 46), (255, 255, 255))
        draw = ImageDraw.Draw(image, "RGBA")
        draw.text((18, 12), f"Parameter heatmaps: {initialization}", fill=rgba("#111111"))
        for index, metric in enumerate(heatmap_metrics):
            if metric not in group.columns:
                raise ValueError(f"Heatmap metric is missing: {metric}")
            table = group.pivot_table(index="lambda_c", columns="lambda_r", values=metric, aggfunc="mean")
            table = table.sort_index().sort_index(axis=1)
            draw_heatmap_panel(draw, table, (index * panel_w + 6, 46, (index + 1) * panel_w - 6, panel_h + 46), metric)
        image.save(figures_dir / f"heatmap_{initialization}.png")


def plot_objective_final(manifest: pd.DataFrame, figures_dir: Path, dpi: int) -> None:
    initializations = sorted(manifest["initialization"].dropna().unique().tolist())
    panel_w = 430
    panel_h = 340
    image = Image.new("RGB", (panel_w * len(initializations), panel_h + 46), (255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.text((18, 12), "Final objective by setting", fill=rgba("#111111"))
    for index, initialization in enumerate(initializations):
        group = manifest.loc[manifest["initialization"] == initialization]
        table = group.pivot_table(index="lambda_c", columns="lambda_r", values="objective", aggfunc="mean")
        table = table.sort_index().sort_index(axis=1)
        draw_heatmap_panel(draw, table, (index * panel_w + 6, 46, (index + 1) * panel_w - 6, panel_h + 46), f"Objective: {initialization}")
    image.save(figures_dir / "objective_final_by_setting.png")


def plot_objective_trace(trace: pd.DataFrame, best: BestSelection, figures_dir: Path, dpi: int) -> None:
    selected = trace.loc[
        (trace["initialization"] == best.initialization)
        & (trace["setting_id"] == best.setting_id)
    ].copy()
    if selected.empty:
        raise RuntimeError(f"No objective trace rows found for {best.run_id}")
    columns = [column for column in ["objective", "r_cap", "r_graph", "r_cont", "r_conn"] if column in selected.columns]
    image = Image.new("RGB", (1500, 900), (255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    plot_box = (120, 80, 1400, 780)
    y_values = pd.concat([pd.to_numeric(selected[column], errors="coerce") for column in columns])
    x_bounds = data_bounds(selected["step"])
    y_bounds = data_bounds(y_values)
    draw_axes(draw, plot_box, "Accepted move step", "Objective component value", f"Objective trace: {best.run_id}", x_bounds, y_bounds)
    colors = ["#d7301f", "#2b8cbe", "#31a354", "#756bb1", "#e6550d"]
    for column, color in zip(columns, colors):
        points = [
            chart_transform(row["step"], row[column], x_bounds, y_bounds, plot_box)
            for _, row in selected.dropna(subset=[column]).iterrows()
        ]
        if len(points) >= 2:
            draw.line(points, fill=rgba(color), width=3)
        elif points:
            px, py = points[0]
            draw.ellipse((px - 3, py - 3, px + 3, py + 3), fill=rgba(color))
    legend_x = 1120
    legend_y = 105
    for index, (column, color) in enumerate(zip(columns, colors)):
        y = legend_y + index * 24
        draw.line((legend_x, y, legend_x + 32, y), fill=rgba(color), width=4)
        draw.text((legend_x + 42, y - 8), column, fill=rgba("#333333"))
    image.save(figures_dir / "objective_trace_best.png")


def plot_metric_comparison(metrics: pd.DataFrame, best: BestSelection, config: dict[str, Any], figures_dir: Path, dpi: int) -> None:
    comparison = config["visualization"]["comparison_algorithms"]
    rows = metrics.loc[
        ((metrics["source_type"] == "baseline") & metrics["algorithm"].isin(comparison))
        | (metrics["run_id"] == best.run_id)
    ].copy()
    rows["label"] = rows["algorithm"].where(rows["source_type"] == "baseline", "best_regularized")
    metric_names = config["visualization"]["core_metric_comparison"]
    panel_w = 520
    panel_h = 360
    image = Image.new("RGB", (panel_w * 3, panel_h * 2 + 42), (255, 255, 255))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.text((18, 12), "Best regularized result vs baselines", fill=rgba("#111111"))
    for metric_index, metric in enumerate(metric_names):
        if metric not in rows.columns:
            raise ValueError(f"Comparison metric is missing: {metric}")
        row_index = metric_index // 3
        col_index = metric_index % 3
        left = col_index * panel_w + 58
        top = row_index * panel_h + 70
        right = (col_index + 1) * panel_w - 38
        bottom = (row_index + 1) * panel_h + 20
        values = pd.to_numeric(rows[metric], errors="coerce")
        y0, y1 = data_bounds(values)
        y0 = min(0.0, y0)
        draw.rectangle((left, top, right, bottom), outline=rgba("#333333"))
        for grid in range(5):
            y = top + int(round(grid * (bottom - top) / 4))
            draw.line((left, y, right, y), fill=rgba("#dddddd"))
        bar_gap = 12
        bar_w = max(20, int((right - left - bar_gap * (len(rows) + 1)) / len(rows)))
        for index, (_, row) in enumerate(rows.iterrows()):
            value = float(row[metric])
            x0 = left + bar_gap + index * (bar_w + bar_gap)
            bar_top = bottom - int(round((value - y0) / (y1 - y0) * (bottom - top)))
            color = rgba("#d7301f") if row["label"] == "best_regularized" else rgba("#636363")
            draw.rectangle((x0, bar_top, x0 + bar_w, bottom), fill=color)
            draw.text((x0 - 4, bottom + 8), str(row["label"])[:12], fill=rgba("#333333"))
        draw.text((left, top - 24), metric, fill=rgba("#111111"))
    image.save(figures_dir / "best_vs_baselines_metrics.png")


def verify_pngs(figures_dir: Path, expected: list[str]) -> None:
    missing = []
    empty = []
    for name in expected:
        path = figures_dir / name
        if not path.exists():
            missing.append(name)
        elif path.stat().st_size <= 0:
            empty.append(name)
    if missing or empty:
        raise RuntimeError(f"Visualization verification failed. missing={missing}, empty={empty}")


def write_color_diagnostics(
    clusters: gpd.GeoDataFrame,
    graph: nx.Graph,
    config: dict[str, Any],
    output_path: Path,
) -> None:
    cluster_ids = sorted(clusters["cluster_id"].dropna().unique().tolist(), key=lambda value: str(value))
    color_map, adjacency = make_cluster_color_map(cluster_ids, clusters, graph, config)
    diagnostics = cluster_color_diagnostics(cluster_ids, color_map, adjacency)
    diagnostics.to_csv(output_path, index=False)
    threshold = float(config["visualization"].get("min_color_distance", 90.0))
    finite = pd.to_numeric(diagnostics["min_adjacent_color_distance"], errors="coerce").dropna()
    if not finite.empty and float(finite.min()) < threshold:
        print(
            "Warning: minimum adjacent cluster color distance "
            f"{float(finite.min()):.2f} is below configured threshold {threshold:.2f}."
        )


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv[1:]
    config_path = project_path(argv[0]) if argv else Path(__file__).with_name("config_v1.yaml")
    config = load_config(config_path)
    run_root, tables_dir, figures_dir = resolve_run_paths(config)
    validate_inputs(config, tables_dir)

    metrics = pd.read_csv(tables_dir / "metrics_regularized.csv")
    manifest = pd.read_csv(tables_dir / "run_manifest.csv")
    trace = pd.read_csv(tables_dir / "objective_trace.csv")
    metrics = add_balanced_score(metrics, config)
    best = select_best_run(metrics, manifest, config)
    write_best_summary(best, metrics, tables_dir / "best_selection_summary.csv")
    metrics.to_csv(tables_dir / "metrics_regularized_with_scores.csv", index=False)

    dpi = int(config["visualization"]["dpi"])
    linewidth = float(config["visualization"]["linewidth"])
    map_size_px = int(config["visualization"]["map_size_px"])
    panel_size_px = int(config["visualization"]["panel_map_size_px"])

    classified = gpd.read_file(project_path(config["inputs"]["classified_edges"]))
    boundary = gpd.read_file(project_path(config["inputs"]["boundary"]))
    connectors = classified.loc[classified["segment_role"] == "connector"].copy()
    graph = load_graph(project_path(config["inputs"]["graph"]))

    best_clusters = gpd.read_file(best.clusters_gpkg)
    if "seg_id" not in best_clusters.columns or "cluster_id" not in best_clusters.columns:
        raise ValueError(f"Selected cluster file lacks seg_id/cluster_id columns: {best.clusters_gpkg}")

    best_map = figures_dir / "best_partition_map.png"
    plot_cluster_map(
        best_clusters,
        connectors,
        boundary,
        graph,
        config,
        best_map,
        map_size_px,
        linewidth,
        dpi,
        f"Best regularized: {best.run_id}",
    )

    comparison_panels = []
    for algorithm in config["visualization"]["baseline_algorithms"]:
        comparison_panels.append((algorithm, project_path(config["inputs"]["baseline_clusters"][algorithm])))
    comparison_panels.append(("best_regularized", best.clusters_gpkg))
    plot_comparison_maps(
        comparison_panels,
        connectors,
        boundary,
        graph,
        config,
        figures_dir / "baseline_vs_best_maps.png",
        panel_size_px,
        linewidth,
        dpi,
    )

    projected_zoom = project_bounds(
        config["visualization"]["zoom_bounds"],
        "EPSG:4326",
        str(best_clusters.crs),
    )
    plot_zoom_map(
        best_clusters,
        connectors,
        graph,
        boundary,
        config,
        figures_dir / "best_connector_zoom.png",
        projected_zoom,
        panel_size_px,
        linewidth,
        dpi,
    )

    for pair in config["visualization"]["tradeoff_pairs"]:
        plot_tradeoff(metrics, best, pair, figures_dir / f"tradeoff_{pair['name']}.png", dpi)
    plot_heatmaps(metrics, config, figures_dir, dpi)
    plot_objective_trace(trace, best, figures_dir, dpi)
    plot_objective_final(manifest, figures_dir, dpi)
    plot_metric_comparison(metrics, best, config, figures_dir, dpi)
    write_color_diagnostics(best_clusters, graph, config, tables_dir / "cluster_color_diagnostics_best.csv")

    expected = [
        "best_partition_map.png",
        "baseline_vs_best_maps.png",
        "best_connector_zoom.png",
        "objective_trace_best.png",
        "objective_final_by_setting.png",
        "best_vs_baselines_metrics.png",
        *[f"tradeoff_{pair['name']}.png" for pair in config["visualization"]["tradeoff_pairs"]],
        *[f"heatmap_{initialization}.png" for initialization in sorted(metrics.loc[metrics["source_type"] == "regularized", "initialization"].dropna().unique())],
    ]
    verify_pngs(figures_dir, expected)
    print(f"Selected best run: {best.run_id} (balanced_score={best.balanced_score:.6f})")
    print(f"Saved figures to {figures_dir}")
    print(f"Saved best summary to {tables_dir / 'best_selection_summary.csv'}")


if __name__ == "__main__":
    main()
