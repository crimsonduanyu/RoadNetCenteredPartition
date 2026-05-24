from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

import env_setup  # noqa: F401
import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import Point
from shapely.ops import unary_union

from utils_geo import PROJECT_ROOT, project_gdf


EPS = 1e-9


@dataclass(frozen=True)
class MetricThresholds:
    small_cluster_min_segments: int = 5
    small_cluster_min_length_m: float = 1000.0
    small_cluster_min_orders: float = 100.0
    min_order_total: float = 100.0
    capacity_min_ratio: float = 0.5
    capacity_max_ratio: float = 1.5
    effective_od_min_flow: float = 10.0
    elongation_threshold: float = 10.0
    large_diameter_threshold_m: float = 30000.0
    geometry_buffer_m: float = 50.0


def safe_divide(numerator: float, denominator: float, default: float = np.nan) -> float:
    if denominator is None or abs(float(denominator)) <= EPS:
        return float(default)
    return float(numerator) / float(denominator)


def coefficient_of_variation(values: pd.Series) -> float:
    values = pd.to_numeric(values, errors="coerce").dropna()
    if values.empty:
        return float("nan")
    mean = float(values.mean())
    if abs(mean) <= EPS:
        return float("nan")
    return float(values.std(ddof=0) / mean)


def bool_series(series: pd.Series) -> pd.Series:
    if series.dtype == bool:
        return series.fillna(False)
    return series.fillna(False).astype(str).str.lower().isin({"true", "1", "yes"})


def nonempty_name(series: pd.Series) -> pd.Series:
    return series.notna() & (series.astype(str).str.strip() != "")


def weighted_quantities(values: pd.Series) -> dict[str, float]:
    values = pd.to_numeric(values, errors="coerce").dropna()
    if values.empty:
        return {
            "mean": float("nan"),
            "min": float("nan"),
            "max": float("nan"),
            "median": float("nan"),
        }
    return {
        "mean": float(values.mean()),
        "min": float(values.min()),
        "max": float(values.max()),
        "median": float(values.median()),
    }


def merge_optional_features(
    clusters: gpd.GeoDataFrame,
    poi_features: pd.DataFrame | None,
    order_features: pd.DataFrame | None,
) -> gpd.GeoDataFrame:
    merged = clusters.copy()
    if poi_features is not None and not poi_features.empty:
        merged = merged.merge(poi_features, on="seg_id", how="left", suffixes=("", "_poi"))
    if order_features is not None and not order_features.empty:
        merged = merged.merge(order_features, on="seg_id", how="left", suffixes=("", "_order"))

    for column in merged.columns:
        if column.startswith("poi_cat_") or column in {
            "poi_total",
            "pickup_count",
            "dropoff_count",
            "order_total",
            "pickup_dropoff_imbalance",
            "morning_peak_pickups",
            "evening_peak_pickups",
            "night_pickups",
            "weekday_pickups",
            "weekend_pickups",
            "weekday_weekend_diff",
        }:
            merged[column] = pd.to_numeric(merged[column], errors="coerce").fillna(0)
    return merged


def build_cluster_table(clusters: gpd.GeoDataFrame) -> pd.DataFrame:
    aggregations: dict[str, tuple[str, str]] = {
        "num_segments": ("seg_id", "count"),
        "total_length_m": ("length", "sum"),
    }
    for column in ["pickup_count", "dropoff_count", "order_total", "poi_total"]:
        if column in clusters.columns:
            aggregations[column] = (column, "sum")
    table = clusters.groupby("cluster_id").agg(**aggregations).reset_index()
    for column in ["pickup_count", "dropoff_count", "order_total", "poi_total"]:
        if column not in table.columns:
            table[column] = np.nan
    return table


def partition_from_clusters(clusters: gpd.GeoDataFrame) -> dict[str, Any]:
    return dict(zip(clusters["seg_id"].astype(str), clusters["cluster_id"]))


def edge_cut_frame(edges: pd.DataFrame, partition: dict[str, Any]) -> pd.DataFrame:
    frame = edges.copy()
    frame["cluster_a"] = frame["seg_id_a"].astype(str).map(partition)
    frame["cluster_b"] = frame["seg_id_b"].astype(str).map(partition)
    frame = frame.dropna(subset=["cluster_a", "cluster_b"]).copy()
    frame["is_cut"] = frame["cluster_a"] != frame["cluster_b"]
    return frame


def weighted_edge_cut_metrics(
    edges: pd.DataFrame,
    mask_column: str,
    weight_column: str,
    prefix: str,
) -> dict[str, float]:
    if mask_column not in edges.columns:
        return {
            f"{prefix}_edge_cut_ratio": float("nan"),
            f"{prefix}_edge_cut_weight": float("nan"),
            f"{prefix}_edge_total_weight": float("nan"),
        }
    selected = edges.loc[bool_series(edges[mask_column])].copy()
    if selected.empty:
        return {
            f"{prefix}_edge_cut_ratio": float("nan"),
            f"{prefix}_edge_cut_weight": 0.0,
            f"{prefix}_edge_total_weight": 0.0,
        }
    weights = pd.to_numeric(selected.get(weight_column, selected.get("weight")), errors="coerce").fillna(0)
    total_weight = float(weights.sum())
    cut_weight = float(weights.loc[selected["is_cut"]].sum())
    return {
        f"{prefix}_edge_cut_ratio": safe_divide(cut_weight, total_weight),
        f"{prefix}_edge_cut_weight": cut_weight,
        f"{prefix}_edge_total_weight": total_weight,
    }


def unweighted_edge_cut_ratio(edges: pd.DataFrame, mask: pd.Series) -> float:
    selected = edges.loc[mask].copy()
    if selected.empty:
        return float("nan")
    return float(selected["is_cut"].mean())


def connectivity_metrics(graph: nx.Graph, partition: dict[str, Any]) -> dict[str, float]:
    cluster_to_nodes: dict[Any, list[str]] = {}
    for node, cluster_id in partition.items():
        cluster_to_nodes.setdefault(cluster_id, []).append(node)

    if not cluster_to_nodes:
        return {
            "connected_cluster_ratio": float("nan"),
            "total_fragment_count": float("nan"),
            "mean_components_per_cluster": float("nan"),
            "max_components_per_cluster": float("nan"),
            "mean_largest_cc_ratio": float("nan"),
            "min_largest_cc_ratio": float("nan"),
        }

    connected_count = 0
    component_counts = []
    largest_ratios = []
    for nodes in cluster_to_nodes.values():
        subgraph = graph.subgraph(nodes)
        if subgraph.number_of_nodes() == 0:
            component_counts.append(0)
            largest_ratios.append(float("nan"))
            continue
        components = list(nx.connected_components(subgraph))
        component_counts.append(len(components))
        if len(components) == 1:
            connected_count += 1
        largest_ratios.append(max(len(component) for component in components) / len(nodes))

    counts = pd.Series(component_counts, dtype=float)
    ratios = pd.Series(largest_ratios, dtype=float).dropna()
    return {
        "connected_cluster_ratio": float(connected_count / len(cluster_to_nodes)),
        "total_fragment_count": float(sum(max(count - 1, 0) for count in component_counts)),
        "mean_components_per_cluster": float(counts.mean()),
        "max_components_per_cluster": float(counts.max()),
        "mean_largest_cc_ratio": float(ratios.mean()) if not ratios.empty else float("nan"),
        "min_largest_cc_ratio": float(ratios.min()) if not ratios.empty else float("nan"),
    }


def road_integrity_metrics(clusters: gpd.GeoDataFrame, cut_edges: pd.DataFrame) -> dict[str, float]:
    metrics = {
        "mean_road_name_num_clusters": float("nan"),
        "weighted_mean_road_name_num_clusters": float("nan"),
        "mean_dominant_cluster_share": float("nan"),
        "weighted_mean_dominant_cluster_share": float("nan"),
        "same_road_adjacent_cut_ratio": float("nan"),
    }
    named = clusters.loc[nonempty_name(clusters["name"])].copy() if "name" in clusters.columns else clusters.iloc[0:0].copy()
    if not named.empty:
        road_cluster_length = (
            named.groupby(["name", "cluster_id"])["length"]
            .sum()
            .rename("cluster_length_m")
            .reset_index()
        )
        road_total = road_cluster_length.groupby("name")["cluster_length_m"].sum().rename("road_length_m")
        road_splits = road_cluster_length.groupby("name")["cluster_id"].nunique().rename("num_clusters")
        dominant = road_cluster_length.groupby("name")["cluster_length_m"].max().rename("dominant_length_m")
        road_stats = pd.concat([road_total, road_splits, dominant], axis=1).reset_index()
        road_stats["dominant_share"] = road_stats["dominant_length_m"] / (road_stats["road_length_m"] + EPS)
        total_length = float(road_stats["road_length_m"].sum())
        metrics.update(
            {
                "mean_road_name_num_clusters": float(road_stats["num_clusters"].mean()),
                "weighted_mean_road_name_num_clusters": safe_divide(
                    float((road_stats["road_length_m"] * road_stats["num_clusters"]).sum()),
                    total_length,
                ),
                "mean_dominant_cluster_share": float(road_stats["dominant_share"].mean()),
                "weighted_mean_dominant_cluster_share": safe_divide(
                    float((road_stats["road_length_m"] * road_stats["dominant_share"]).sum()),
                    total_length,
                ),
            }
        )

    if "same_name" in cut_edges.columns:
        metrics["same_road_adjacent_cut_ratio"] = unweighted_edge_cut_ratio(cut_edges, bool_series(cut_edges["same_name"]))
    return metrics


def rectangle_elongation(geometry) -> float:
    if geometry is None or geometry.is_empty:
        return float("nan")
    rectangle = geometry.minimum_rotated_rectangle
    if rectangle.geom_type != "Polygon":
        return 1.0
    coords = list(rectangle.exterior.coords)
    if len(coords) < 4:
        return 1.0
    lengths = []
    for first, second in zip(coords[:4], coords[1:5]):
        lengths.append(math.dist(first, second))
    positive = [length for length in lengths if length > EPS]
    if not positive:
        return 1.0
    return float(max(positive) / (min(positive) + EPS))


def shape_metrics(clusters: gpd.GeoDataFrame, thresholds: MetricThresholds) -> dict[str, float]:
    values = []
    for _, group in clusters.groupby("cluster_id"):
        buffered = [geometry.buffer(thresholds.geometry_buffer_m) for geometry in group.geometry if geometry is not None and not geometry.is_empty]
        if not buffered:
            continue
        polygon = unary_union(buffered)
        if polygon.is_empty:
            continue
        area = float(polygon.area)
        perimeter = float(polygon.length)
        hull_area = float(polygon.convex_hull.area)
        compactness = safe_divide(4.0 * math.pi * area, perimeter * perimeter)
        hull_ratio = safe_divide(area, hull_area)
        elongation = rectangle_elongation(polygon)
        boundary_complexity = safe_divide(perimeter, math.sqrt(area + EPS))
        values.append(
            {
                "compactness": compactness,
                "convex_hull_ratio": hull_ratio,
                "elongation": elongation,
                "boundary_complexity": boundary_complexity,
            }
        )

    frame = pd.DataFrame(values)
    if frame.empty:
        return {
            "mean_compactness": float("nan"),
            "median_compactness": float("nan"),
            "min_compactness": float("nan"),
            "mean_convex_hull_ratio": float("nan"),
            "median_convex_hull_ratio": float("nan"),
            "min_convex_hull_ratio": float("nan"),
            "mean_elongation": float("nan"),
            "median_elongation": float("nan"),
            "max_elongation": float("nan"),
            "elongated_cluster_ratio": float("nan"),
            "mean_boundary_complexity": float("nan"),
        }
    return {
        "mean_compactness": float(frame["compactness"].mean()),
        "median_compactness": float(frame["compactness"].median()),
        "min_compactness": float(frame["compactness"].min()),
        "mean_convex_hull_ratio": float(frame["convex_hull_ratio"].mean()),
        "median_convex_hull_ratio": float(frame["convex_hull_ratio"].median()),
        "min_convex_hull_ratio": float(frame["convex_hull_ratio"].min()),
        "mean_elongation": float(frame["elongation"].mean()),
        "median_elongation": float(frame["elongation"].median()),
        "max_elongation": float(frame["elongation"].max()),
        "elongated_cluster_ratio": float((frame["elongation"] > thresholds.elongation_threshold).mean()),
        "mean_boundary_complexity": float(frame["boundary_complexity"].mean()),
    }


def network_diameter_metrics(
    graph: nx.Graph,
    clusters: gpd.GeoDataFrame,
    partition: dict[str, Any],
    thresholds: MetricThresholds,
) -> dict[str, float]:
    lengths = clusters.set_index("seg_id")["length"].astype(float).to_dict()
    cluster_to_nodes: dict[Any, list[str]] = {}
    for node, cluster_id in partition.items():
        cluster_to_nodes.setdefault(cluster_id, []).append(node)

    def edge_distance(u: str, v: str, attrs: dict[str, Any]) -> float:
        return max((float(lengths.get(u, 0.0)) + float(lengths.get(v, 0.0))) / 2.0, EPS)

    diameters = []
    for nodes in cluster_to_nodes.values():
        subgraph = graph.subgraph(nodes)
        component_diameters = []
        for component in nx.connected_components(subgraph):
            component_nodes = list(component)
            if len(component_nodes) <= 1:
                component_diameters.append(0.0)
                continue
            start = component_nodes[0]
            first_lengths = nx.single_source_dijkstra_path_length(subgraph, start, weight=edge_distance)
            farthest = max(first_lengths, key=first_lengths.get)
            second_lengths = nx.single_source_dijkstra_path_length(subgraph, farthest, weight=edge_distance)
            component_diameters.append(float(max(second_lengths.values())))
        diameters.append(max(component_diameters) if component_diameters else 0.0)

    series = pd.Series(diameters, dtype=float)
    if series.empty:
        return {
            "mean_network_diameter_m": float("nan"),
            "median_network_diameter_m": float("nan"),
            "max_network_diameter_m": float("nan"),
            "large_diameter_cluster_ratio": float("nan"),
        }
    return {
        "mean_network_diameter_m": float(series.mean()),
        "median_network_diameter_m": float(series.median()),
        "max_network_diameter_m": float(series.max()),
        "large_diameter_cluster_ratio": float((series > thresholds.large_diameter_threshold_m).mean()),
    }


def wss_metrics(features: pd.DataFrame | None, partition: dict[str, Any], columns: list[str], prefix: str) -> dict[str, float]:
    if features is None or features.empty:
        return {f"{prefix}_wss": float("nan"), f"{prefix}_wss_per_segment": float("nan")}
    available = [column for column in columns if column in features.columns]
    if not available:
        return {f"{prefix}_wss": float("nan"), f"{prefix}_wss_per_segment": float("nan")}
    frame = features[["seg_id", *available]].copy()
    frame["cluster_id"] = frame["seg_id"].astype(str).map(partition)
    frame = frame.dropna(subset=["cluster_id"]).copy()
    if frame.empty:
        return {f"{prefix}_wss": float("nan"), f"{prefix}_wss_per_segment": float("nan")}
    values = frame[available].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    means = values.groupby(frame["cluster_id"]).transform("mean")
    wss = float(((values - means) ** 2).to_numpy().sum())
    return {f"{prefix}_wss": wss, f"{prefix}_wss_per_segment": safe_divide(wss, len(frame))}


def poi_cluster_metrics(clusters: gpd.GeoDataFrame, poi_category_cols: list[str]) -> dict[str, float]:
    if not poi_category_cols:
        return {
            "mean_cluster_poi_entropy": float("nan"),
            "weighted_mean_cluster_poi_entropy": float("nan"),
            "mean_dominant_poi_purity": float("nan"),
            "weighted_mean_dominant_poi_purity": float("nan"),
        }
    counts = clusters.groupby("cluster_id")[poi_category_cols].sum()
    totals = counts.sum(axis=1)
    probabilities = counts.div(totals.replace(0, np.nan), axis=0).fillna(0.0)
    entropy = -(probabilities * np.log(probabilities + EPS)).sum(axis=1)
    purity = probabilities.max(axis=1)
    total_poi = float(totals.sum())
    return {
        "mean_cluster_poi_entropy": float(entropy.mean()),
        "weighted_mean_cluster_poi_entropy": safe_divide(float((entropy * totals).sum()), total_poi),
        "mean_dominant_poi_purity": float(purity.mean()),
        "weighted_mean_dominant_poi_purity": safe_divide(float((purity * totals).sum()), total_poi),
    }


def order_cluster_metrics(clusters: gpd.GeoDataFrame) -> dict[str, float]:
    if "pickup_count" not in clusters.columns or "dropoff_count" not in clusters.columns:
        return {"mean_within_cluster_imbalance_var": float("nan")}
    pickup = pd.to_numeric(clusters["pickup_count"], errors="coerce").fillna(0.0)
    dropoff = pd.to_numeric(clusters["dropoff_count"], errors="coerce").fillna(0.0)
    clusters = clusters.copy()
    clusters["normalized_imbalance"] = (pickup - dropoff) / (pickup + dropoff + EPS)
    variance = clusters.groupby("cluster_id")["normalized_imbalance"].var(ddof=0)
    return {"mean_within_cluster_imbalance_var": float(variance.mean()) if not variance.empty else float("nan")}


def od_metrics(
    hourly_od: pd.DataFrame | None,
    partition: dict[str, Any],
    cluster_ids: list[Any],
    thresholds: MetricThresholds,
) -> dict[str, float]:
    metric_names = {
        "od_sparsity": float("nan"),
        "mean_nonzero_od_pairs_per_slot": float("nan"),
        "mean_nonzero_od_ratio_per_slot": float("nan"),
        "mean_orders_per_nonzero_od": float("nan"),
        "median_orders_per_nonzero_od": float("nan"),
        "num_effective_od_pairs": float("nan"),
        "effective_od_pair_ratio": float("nan"),
        "od_flow_hhi": float("nan"),
        "historical_avg_mae": float("nan"),
        "historical_avg_rmse": float("nan"),
        "historical_avg_wape": float("nan"),
    }
    if hourly_od is None or hourly_od.empty or not cluster_ids:
        return metric_names

    frame = hourly_od.copy()
    frame["slot_start"] = pd.to_datetime(frame["slot_start"], errors="coerce")
    frame["origin_cluster"] = frame["origin_seg_id"].astype(str).map(partition)
    frame["destination_cluster"] = frame["destination_seg_id"].astype(str).map(partition)
    frame = frame.dropna(subset=["slot_start", "origin_cluster", "destination_cluster"]).copy()
    if frame.empty:
        return metric_names
    frame["order_count"] = pd.to_numeric(frame["order_count"], errors="coerce").fillna(0.0)
    cluster_od = (
        frame.groupby(["slot_start", "origin_cluster", "destination_cluster"], observed=True)["order_count"]
        .sum()
        .reset_index()
    )
    possible_pairs = len(cluster_ids) * len(cluster_ids)
    if possible_pairs <= 0:
        return metric_names

    nonzero_by_slot = cluster_od.groupby("slot_start").size()
    nonzero_counts = cluster_od["order_count"].loc[cluster_od["order_count"] > 0]
    cumulative = cluster_od.groupby(["origin_cluster", "destination_cluster"], observed=True)["order_count"].sum()
    total_flow = float(cumulative.sum())
    shares = cumulative / (total_flow + EPS)

    metric_names.update(
        {
            "od_sparsity": float(1.0 - (nonzero_by_slot / possible_pairs).mean()),
            "mean_nonzero_od_pairs_per_slot": float(nonzero_by_slot.mean()),
            "mean_nonzero_od_ratio_per_slot": float((nonzero_by_slot / possible_pairs).mean()),
            "mean_orders_per_nonzero_od": float(nonzero_counts.mean()) if not nonzero_counts.empty else float("nan"),
            "median_orders_per_nonzero_od": float(nonzero_counts.median()) if not nonzero_counts.empty else float("nan"),
            "num_effective_od_pairs": float((cumulative >= thresholds.effective_od_min_flow).sum()),
            "effective_od_pair_ratio": float((cumulative >= thresholds.effective_od_min_flow).sum() / possible_pairs),
            "od_flow_hhi": float((shares**2).sum()) if total_flow > 0 else float("nan"),
        }
    )
    metric_names.update(historical_average_metrics(cluster_od, cluster_ids))
    return metric_names


def historical_average_metrics(cluster_od: pd.DataFrame, cluster_ids: list[Any]) -> dict[str, float]:
    result = {
        "historical_avg_mae": float("nan"),
        "historical_avg_rmse": float("nan"),
        "historical_avg_wape": float("nan"),
    }
    frame = cluster_od.copy()
    frame["date"] = frame["slot_start"].dt.date
    frame["hour"] = frame["slot_start"].dt.hour
    workday_dates = sorted(frame["date"].unique())
    if len(workday_dates) < 2:
        return result

    train_dates = workday_dates[:4] if len(workday_dates) >= 5 else workday_dates[:-1]
    test_dates = [workday_dates[-1]]
    if not train_dates:
        return result

    train = frame.loc[frame["date"].isin(train_dates)].copy()
    test = frame.loc[frame["date"].isin(test_dates)].copy()
    if test.empty:
        return result

    train_sum = train.groupby(["hour", "origin_cluster", "destination_cluster"], observed=True)["order_count"].sum()
    prediction = train_sum / len(train_dates)
    actual = test.groupby(["hour", "origin_cluster", "destination_cluster"], observed=True)["order_count"].sum()

    full_index = pd.MultiIndex.from_product(
        [range(24), cluster_ids, cluster_ids],
        names=["hour", "origin_cluster", "destination_cluster"],
    )
    pred_values = prediction.reindex(full_index, fill_value=0.0)
    actual_values = actual.reindex(full_index, fill_value=0.0)
    errors = actual_values - pred_values
    absolute = errors.abs()
    result.update(
        {
            "historical_avg_mae": float(absolute.mean()),
            "historical_avg_rmse": float(np.sqrt((errors**2).mean())),
            "historical_avg_wape": safe_divide(float(absolute.sum()), float(actual_values.abs().sum())),
        }
    )
    return result


def connector_type_metrics(edges: pd.DataFrame, graph_variant: str, algorithm: str) -> pd.DataFrame:
    if "has_connector" not in edges.columns or "connector_highways" not in edges.columns:
        return pd.DataFrame(
            columns=[
                "graph_variant",
                "algorithm",
                "connector_type",
                "total_edges",
                "cut_edges",
                "cut_ratio",
                "cut_weight_ratio",
            ]
        )
    selected = edges.loc[bool_series(edges["has_connector"])].copy()
    if selected.empty:
        return pd.DataFrame()
    rows = []
    for _, row in selected.iterrows():
        value = row.get("connector_highways")
        connector_types = [item for item in str(value).split("|") if item and item.lower() != "nan"]
        if not connector_types:
            connector_types = ["unknown"]
        for connector_type in connector_types:
            rows.append(
                {
                    "connector_type": connector_type,
                    "is_cut": bool(row["is_cut"]),
                    "weight": float(row.get("connector_weight", row.get("weight", 0.0)) or 0.0),
                }
            )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    summaries = []
    for connector_type, group in frame.groupby("connector_type"):
        total_weight = float(group["weight"].sum())
        cut_weight = float(group.loc[group["is_cut"], "weight"].sum())
        summaries.append(
            {
                "graph_variant": graph_variant,
                "algorithm": algorithm,
                "connector_type": connector_type,
                "total_edges": int(len(group)),
                "cut_edges": int(group["is_cut"].sum()),
                "cut_ratio": float(group["is_cut"].mean()),
                "cut_weight_ratio": safe_divide(cut_weight, total_weight),
            }
        )
    return pd.DataFrame(summaries).sort_values(["graph_variant", "algorithm", "connector_type"])


def compute_benchmark_metrics(
    graph_variant: str,
    algorithm: str,
    params: str,
    clusters: gpd.GeoDataFrame,
    relation_edges: pd.DataFrame,
    graph: nx.Graph,
    poi_features: pd.DataFrame | None = None,
    order_features: pd.DataFrame | None = None,
    hourly_od: pd.DataFrame | None = None,
    thresholds: MetricThresholds | None = None,
) -> tuple[dict[str, Any], pd.DataFrame]:
    thresholds = thresholds or MetricThresholds()
    clusters = merge_optional_features(clusters, poi_features, order_features)
    clusters["seg_id"] = clusters["seg_id"].astype(str)
    partition = partition_from_clusters(clusters)
    cluster_ids = sorted(clusters["cluster_id"].dropna().unique().tolist())
    cluster_table = build_cluster_table(clusters)
    cut_edges = edge_cut_frame(relation_edges, partition)

    segment_stats = weighted_quantities(cluster_table["num_segments"])
    length_stats = weighted_quantities(cluster_table["total_length_m"])
    small_mask = (
        (cluster_table["num_segments"] < thresholds.small_cluster_min_segments)
        | (cluster_table["total_length_m"] < thresholds.small_cluster_min_length_m)
        | (cluster_table["order_total"].fillna(np.inf) < thresholds.small_cluster_min_orders)
    )
    low_order_mask = cluster_table["order_total"].fillna(np.inf) < thresholds.min_order_total
    order_totals = pd.to_numeric(cluster_table["order_total"], errors="coerce").dropna()
    if order_totals.empty or float(order_totals.sum()) <= EPS:
        target_order_capacity = float("nan")
        capacity_violation_ratio = float("nan")
        capacity_hinge_loss = float("nan")
    else:
        target_order_capacity = float(order_totals.sum() / max(len(order_totals), 1))
        capacity_min = thresholds.capacity_min_ratio * target_order_capacity
        capacity_max = thresholds.capacity_max_ratio * target_order_capacity
        capacity_violation_ratio = float(((order_totals < capacity_min) | (order_totals > capacity_max)).mean())
        low_violation = ((capacity_min - order_totals).clip(lower=0.0) / max(target_order_capacity, EPS)) ** 2
        high_violation = ((order_totals - capacity_max).clip(lower=0.0) / max(target_order_capacity, EPS)) ** 2
        capacity_hinge_loss = float((low_violation + high_violation).mean())

    row: dict[str, Any] = {
        "graph_variant": graph_variant,
        "algorithm": algorithm,
        "params": params,
        "num_clusters": int(len(cluster_ids)),
        "mean_segments_per_cluster": segment_stats["mean"],
        "min_segments_per_cluster": segment_stats["min"],
        "max_segments_per_cluster": segment_stats["max"],
        "median_segments_per_cluster": segment_stats["median"],
        "mean_cluster_length_m": length_stats["mean"],
        "min_cluster_length_m": length_stats["min"],
        "max_cluster_length_m": length_stats["max"],
        "median_cluster_length_m": length_stats["median"],
        "small_cluster_ratio": float(small_mask.mean()) if len(small_mask) else float("nan"),
    }

    row.update(connectivity_metrics(graph, partition))
    row.update(road_integrity_metrics(clusters, cut_edges))
    row.update(weighted_edge_cut_metrics(cut_edges, "has_continuity", "continuity_weight", "continuity"))
    row.update(weighted_edge_cut_metrics(cut_edges, "has_connector", "connector_weight", "connector"))
    row.update(weighted_edge_cut_metrics(cut_edges, "has_direct", "direct_weight", "direct"))
    row.update(shape_metrics(clusters, thresholds))
    row.update(network_diameter_metrics(graph, clusters, partition, thresholds))
    row.update(
        {
            "segment_count_cv": coefficient_of_variation(cluster_table["num_segments"]),
            "road_length_cv": coefficient_of_variation(cluster_table["total_length_m"]),
            "pickup_count_cv": coefficient_of_variation(cluster_table["pickup_count"]),
            "dropoff_count_cv": coefficient_of_variation(cluster_table["dropoff_count"]),
            "order_count_cv": coefficient_of_variation(cluster_table["order_total"]),
            "poi_count_cv": coefficient_of_variation(cluster_table["poi_total"]),
            "clusters_below_min_order": int(low_order_mask.sum()) if len(low_order_mask) else 0,
            "ratio_clusters_below_min_order": float(low_order_mask.mean()) if len(low_order_mask) else float("nan"),
            "target_order_capacity": target_order_capacity,
            "capacity_violation_ratio": capacity_violation_ratio,
            "capacity_hinge_loss": capacity_hinge_loss,
        }
    )

    poi_category_cols = [column for column in clusters.columns if column.startswith("poi_cat_")]
    row.update(wss_metrics(poi_features, partition, poi_category_cols, "poi"))
    row.update(poi_cluster_metrics(clusters, poi_category_cols))
    order_cols = [
        "pickup_count",
        "dropoff_count",
        "pickup_dropoff_imbalance",
        "morning_peak_pickups",
        "evening_peak_pickups",
        "night_pickups",
        "weekday_pickups",
        "weekend_pickups",
        "weekday_weekend_diff",
    ]
    row.update(wss_metrics(order_features, partition, order_cols, "order"))
    row.update(order_cluster_metrics(clusters))
    row.update(od_metrics(hourly_od, partition, cluster_ids, thresholds))

    return row, connector_type_metrics(cut_edges, graph_variant, algorithm)


def match_points_to_segments(
    frame: pd.DataFrame,
    lon_col: str,
    lat_col: str,
    segments: gpd.GeoDataFrame,
    source_crs: str,
    max_distance_m: float,
) -> pd.Series:
    matches = pd.Series(pd.NA, index=frame.index, dtype="object")
    valid = frame[[lon_col, lat_col]].notna().all(axis=1)
    valid &= np.isfinite(frame[lon_col]) & np.isfinite(frame[lat_col])
    if not bool(valid.any()):
        return matches

    points = gpd.GeoDataFrame(
        {"row_id": frame.index[valid]},
        geometry=[Point(xy) for xy in zip(frame.loc[valid, lon_col], frame.loc[valid, lat_col])],
        crs=source_crs,
    ).to_crs(segments.crs)

    joined = gpd.sjoin_nearest(
        points,
        segments[["seg_id", "geometry"]],
        how="left",
        max_distance=max_distance_m,
        distance_col="match_distance_m",
    )
    matched = joined.dropna(subset=["seg_id"]).drop_duplicates("row_id")
    matches.loc[matched["row_id"].to_numpy()] = matched["seg_id"].to_numpy()
    return matches


def load_or_build_hourly_segment_od(config: dict[str, Any], paths: dict[str, Path]) -> pd.DataFrame:
    cache_path = paths.get("order_od_hourly", paths["data_processed"] / "segment_order_od_hourly.csv")
    if cache_path.exists():
        print(f"Loading cached hourly segment OD from {cache_path}")
        return pd.read_csv(cache_path, parse_dates=["slot_start"])

    order_config = config["semantic_graph"]["order"]
    order_path = PROJECT_ROOT / order_config["input_path"]
    if not order_path.exists():
        print(f"Order file not found at {order_path}; OD metrics will be NaN.")
        return pd.DataFrame(columns=["slot_start", "origin_seg_id", "destination_seg_id", "order_count"])

    segments = gpd.read_file(paths["classified_edges"])
    segments = segments.loc[segments["segment_role"] == "ordinary"].copy()
    segments = project_gdf(segments, config["crs"]["projected"]).copy()
    segment_id_set = set(segments["seg_id"].astype(str))

    time_col = order_config["time_column"]
    pickup_lon = order_config["pickup_lon_column"]
    pickup_lat = order_config["pickup_lat_column"]
    dropoff_lon = order_config["dropoff_lon_column"]
    dropoff_lat = order_config["dropoff_lat_column"]
    start_time = pd.Timestamp(order_config["start_time"])
    end_time = pd.Timestamp(order_config["end_time"])
    usecols = [pickup_lon, pickup_lat, dropoff_lon, dropoff_lat, time_col]

    counts: Counter[tuple[pd.Timestamp, str, str]] = Counter()
    total_rows = 0
    window_rows = 0
    weekday_rows = 0
    matched_rows = 0
    print(f"Building hourly segment OD cache from {order_path}")
    for chunk_index, chunk in enumerate(
        pd.read_csv(order_path, usecols=usecols, chunksize=int(order_config["chunksize"])),
        start=1,
    ):
        total_rows += len(chunk)
        chunk[time_col] = pd.to_datetime(chunk[time_col], errors="coerce")
        chunk = chunk.loc[(chunk[time_col] >= start_time) & (chunk[time_col] < end_time)].copy()
        window_rows += len(chunk)
        if chunk.empty:
            continue
        chunk = chunk.loc[chunk[time_col].dt.dayofweek < 5].copy()
        weekday_rows += len(chunk)
        if chunk.empty:
            continue
        chunk = chunk.reset_index(drop=True)
        pickup_seg = match_points_to_segments(
            chunk,
            pickup_lon,
            pickup_lat,
            segments,
            config["crs"]["geographic"],
            float(order_config["max_match_distance_m"]),
        )
        dropoff_seg = match_points_to_segments(
            chunk,
            dropoff_lon,
            dropoff_lat,
            segments,
            config["crs"]["geographic"],
            float(order_config["max_match_distance_m"]),
        )
        valid_od = pickup_seg.notna() & dropoff_seg.notna()
        valid_od &= pickup_seg.astype(str).isin(segment_id_set) & dropoff_seg.astype(str).isin(segment_id_set)
        matched_rows += int(valid_od.sum())
        slots = chunk.loc[valid_od, time_col].dt.floor("h")
        for slot, origin, destination in zip(slots, pickup_seg.loc[valid_od].astype(str), dropoff_seg.loc[valid_od].astype(str)):
            counts[(slot, origin, destination)] += 1

        print(
            "hourly OD chunks: "
            f"{chunk_index:,}, rows read: {total_rows:,}, rows in window: {window_rows:,}, "
            f"weekday rows: {weekday_rows:,}, matched OD rows: {matched_rows:,}"
        )

    rows = [
        {
            "slot_start": slot,
            "origin_seg_id": origin,
            "destination_seg_id": destination,
            "order_count": count,
        }
        for (slot, origin, destination), count in sorted(counts.items())
    ]
    hourly_od = pd.DataFrame(rows, columns=["slot_start", "origin_seg_id", "destination_seg_id", "order_count"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    hourly_od.to_csv(cache_path, index=False)
    print(f"Saved hourly segment OD cache to {cache_path}")
    return hourly_od
