from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import yaml

from roadnet_partition.io.geospatial import PROJECT_ROOT
from roadnet_partition.io.safe_graph import read_safe_graph
from roadnet_partition.zoning.metrics import MetricThresholds, compute_benchmark_metrics
from roadnet_partition.zoning.regularized.selection import baseline_for_algorithm


def project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_evaluation_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_optional_csv(path_value: str | Path | None) -> pd.DataFrame | None:
    if not path_value:
        return None
    path = project_path(path_value)
    if not path.exists():
        return None
    return pd.read_csv(path)


def load_graph(path: Path) -> nx.Graph:
    graph = read_safe_graph(path)
    if any(not isinstance(node, str) for node in graph.nodes):
        graph = nx.relabel_nodes(graph, {node: str(node) for node in graph.nodes})
    return graph

def dominant_value(series: pd.Series):
    series = series.dropna()
    if series.empty:
        return None
    return series.value_counts().idxmax()

def build_cluster_summary(clusters: gpd.GeoDataFrame) -> pd.DataFrame:
    summary = (
        clusters.groupby("cluster_id")
        .agg(
            num_segments=("seg_id", "count"),
            total_length_m=("length", "sum"),
            num_unique_road_names=("name", pd.Series.nunique),
        )
        .reset_index()
    )

    dominant_highway = (
        clusters.groupby("cluster_id")["highway"]
        .apply(dominant_value)
        .rename("dominant_highway")
        .reset_index()
    )
    summary = summary.merge(dominant_highway, on="cluster_id", how="left")
    return summary.sort_values("total_length_m", ascending=False)

def build_road_name_diagnostics(clusters: gpd.GeoDataFrame) -> pd.DataFrame:
    named = clusters.loc[clusters["name"].notna() & (clusters["name"].astype(str).str.strip() != "")].copy()
    diagnostics = (
        named.groupby("name")
        .agg(
            num_segments=("seg_id", "count"),
            total_length_m=("length", "sum"),
            num_clusters=("cluster_id", pd.Series.nunique),
        )
        .reset_index()
    )
    diagnostics = diagnostics.loc[diagnostics["num_segments"] > 1].sort_values(
        ["num_clusters", "total_length_m"],
        ascending=[False, False],
    )
    return diagnostics

def edge_cut_ratio(edges: pd.DataFrame, partition: dict[str, int], mask_column: str) -> float:
    selected = edges.loc[edges[mask_column].fillna(False).astype(bool)].copy()
    if selected.empty:
        return 0.0
    cuts = selected.apply(
        lambda row: partition.get(row["seg_id_a"]) != partition.get(row["seg_id_b"]),
        axis=1,
    )
    return float(cuts.mean())

def cluster_connectivity_ratio(graph: nx.Graph, partition: dict[str, int]) -> tuple[float, float]:
    cluster_to_nodes: dict[int, list[str]] = {}
    for node, cluster_id in partition.items():
        cluster_to_nodes.setdefault(cluster_id, []).append(node)

    connected_count = 0
    component_counts = []
    for nodes in cluster_to_nodes.values():
        subgraph = graph.subgraph(nodes)
        components = nx.number_connected_components(subgraph) if len(subgraph) > 0 else 0
        component_counts.append(components)
        if components == 1:
            connected_count += 1

    if not cluster_to_nodes:
        return 0.0, 0.0
    return float(connected_count / len(cluster_to_nodes)), float(np.mean(component_counts))

def intra_edge_similarity(edges: pd.DataFrame, partition: dict[str, int], similarity_column: str) -> float:
    if similarity_column not in edges.columns or edges.empty:
        return 0.0
    intra = edges.loc[
        edges.apply(lambda row: partition.get(row["seg_id_a"]) == partition.get(row["seg_id_b"]), axis=1)
    ]
    if intra.empty:
        return 0.0
    values = intra[similarity_column].fillna(0).astype(float)
    return float(values.mean())

def cluster_od_sparsity(partition: dict[str, int], paths: dict) -> float:
    od_path = paths["order_od_pairs"]
    if not od_path.exists():
        return 0.0
    od = pd.read_csv(od_path)
    if od.empty:
        return 0.0
    od["origin_cluster"] = od["origin_seg_id"].map(partition)
    od["destination_cluster"] = od["destination_seg_id"].map(partition)
    od = od.dropna(subset=["origin_cluster", "destination_cluster"])
    if od.empty:
        return 0.0
    nonzero_pairs = od[["origin_cluster", "destination_cluster"]].drop_duplicates()
    cluster_count = len(set(partition.values()))
    possible_pairs = cluster_count * cluster_count
    if possible_pairs == 0:
        return 0.0
    density = len(nonzero_pairs) / possible_pairs
    return float(1.0 - density)

def evaluate_partition(
    graph_variant: str,
    algorithm: str,
    graph: nx.Graph,
    edges: pd.DataFrame,
    segments: gpd.GeoDataFrame,
    partition: dict[str, int],
    summary: pd.DataFrame,
    diagnostics: pd.DataFrame,
    paths: dict,
) -> dict:
    connected_ratio, mean_components = cluster_connectivity_ratio(graph, partition)
    total_lengths = summary["total_length_m"].astype(float)
    size_balance_cv = float(total_lengths.std(ddof=0) / total_lengths.mean()) if len(total_lengths) and total_lengths.mean() > 0 else 0.0
    named_road_splits = diagnostics["num_clusters"].astype(float) if not diagnostics.empty else pd.Series(dtype=float)

    return {
        "graph_variant": graph_variant,
        "algorithm": algorithm,
        "num_clusters": int(segments["cluster_id"].nunique()),
        "num_segments": int(len(segments)),
        "mean_cluster_length_m": float(total_lengths.mean()) if len(total_lengths) else 0.0,
        "size_balance_cv": size_balance_cv,
        "connected_cluster_ratio": connected_ratio,
        "mean_components_per_cluster": mean_components,
        "connector_edge_cut_ratio": edge_cut_ratio(edges, partition, "has_connector"),
        "continuity_edge_cut_ratio": edge_cut_ratio(edges, partition, "has_continuity"),
        "mean_clusters_per_named_road": float(named_road_splits.mean()) if not named_road_splits.empty else 0.0,
        "max_clusters_per_named_road": int(named_road_splits.max()) if not named_road_splits.empty else 0,
        "poi_intra_edge_homogeneity": intra_edge_similarity(edges, partition, "poi_similarity"),
        "order_intra_edge_homogeneity": intra_edge_similarity(edges, partition, "order_similarity"),
        "cluster_od_sparsity": cluster_od_sparsity(partition, paths),
    }

def build_ranked_summary(evaluation: pd.DataFrame) -> pd.DataFrame:
    ranked = evaluation.copy()
    ranked["structure_score"] = (
        ranked["connected_cluster_ratio"]
        - ranked["mean_components_per_cluster"].sub(1.0).clip(lower=0)
        - ranked["connector_edge_cut_ratio"]
        - ranked["continuity_edge_cut_ratio"]
    )
    ranked["semantic_score"] = (
        ranked["poi_intra_edge_homogeneity"]
        + ranked["order_intra_edge_homogeneity"]
        + ranked["cluster_od_sparsity"]
    )
    ranked["balance_score"] = -ranked["size_balance_cv"]
    ranked = ranked.sort_values(
        ["structure_score", "semantic_score", "balance_score"],
        ascending=[False, False, False],
    ).reset_index(drop=True)
    ranked.insert(0, "rank", np.arange(1, len(ranked) + 1))
    return ranked

def metric_thresholds(config: dict[str, Any]) -> MetricThresholds:
    values = config.get("evaluation", {}).get("thresholds", {})
    allowed = set(MetricThresholds.__dataclass_fields__)
    return MetricThresholds(**{key: value for key, value in values.items() if key in allowed})

def baseline_params(name: str) -> str:
    return json.dumps({"source": "baseline", "algorithm": name}, sort_keys=True)

def compute_metric_row(
    graph_variant: str,
    algorithm: str,
    params: str,
    clusters_path: Path,
    relation_edges: pd.DataFrame,
    graph: nx.Graph,
    poi_features: pd.DataFrame | None,
    order_features: pd.DataFrame | None,
    hourly_od: pd.DataFrame | None,
    thresholds: MetricThresholds,
) -> tuple[dict[str, Any], pd.DataFrame]:
    clusters = gpd.read_file(clusters_path)
    row, connector_rows = compute_benchmark_metrics(
        graph_variant=graph_variant,
        algorithm=algorithm,
        params=params,
        clusters=clusters,
        relation_edges=relation_edges,
        graph=graph,
        poi_features=poi_features,
        order_features=order_features,
        hourly_od=hourly_od,
        thresholds=thresholds,
    )
    return row, connector_rows

def direction_values(series: pd.Series, direction: str) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    if direction == "min":
        return values
    if direction == "max":
        return -values
    raise ValueError(f"Unknown Pareto direction: {direction}")

def pareto_flags(frame: pd.DataFrame, x: str, y: str, x_direction: str, y_direction: str) -> pd.Series:
    work = pd.DataFrame(
        {
            "x": direction_values(frame[x], x_direction),
            "y": direction_values(frame[y], y_direction),
        },
        index=frame.index,
    ).dropna()
    flags = pd.Series(False, index=frame.index)
    for index, row in work.iterrows():
        others = work.drop(index=index)
        dominated = ((others["x"] <= row["x"]) & (others["y"] <= row["y"]) & ((others["x"] < row["x"]) | (others["y"] < row["y"]))).any()
        flags.loc[index] = not bool(dominated)
    return flags

def build_pareto_summary(metrics: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for pair in config.get("evaluation", {}).get("pareto_pairs", []):
        x = pair["x"]
        y = pair["y"]
        if x not in metrics.columns or y not in metrics.columns:
            continue
        flags = pareto_flags(
            metrics,
            x,
            y,
            pair.get("x_direction", "min"),
            pair.get("y_direction", "min"),
        )
        selected = metrics.loc[flags, ["source_type", "graph_variant", "algorithm", "run_id", x, y]].copy()
        selected.insert(0, "pareto_pair", pair["name"])
        selected = selected.rename(columns={x: "x_value", y: "y_value"})
        selected["x_metric"] = x
        selected["y_metric"] = y
        rows.append(selected)
    if not rows:
        return pd.DataFrame(columns=["pareto_pair", "source_type", "graph_variant", "algorithm", "run_id", "x_value", "y_value", "x_metric", "y_metric"])
    return pd.concat(rows, ignore_index=True)

def relative_not_worse(value: float, baseline: float, max_relative_degradation: float) -> bool:
    if pd.isna(value) or pd.isna(baseline):
        return False
    if abs(float(baseline)) <= 1.0e-12:
        return float(value) <= float(baseline) + max_relative_degradation
    return float(value) <= float(baseline) * (1.0 + max_relative_degradation)

def build_candidate_selection(metrics: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    tolerances = config.get("evaluation", {}).get("selection_tolerances", {})
    od_tol = float(tolerances.get("od_sparsity_max_relative_degradation", 0.02))
    structure_tol = float(tolerances.get("structure_cut_max_relative_degradation", 0.10))
    shape_tol = float(tolerances.get("shape_max_relative_degradation", 0.10))
    baseline_by_algorithm = {
        row["algorithm"]: row
        for _, row in metrics.loc[metrics["source_type"] == "baseline"].iterrows()
    }
    rows = []
    for _, row in metrics.loc[metrics["source_type"] == "regularized"].iterrows():
        baseline_name = baseline_for_algorithm(row["algorithm"])
        baseline = baseline_by_algorithm.get(baseline_name)
        if baseline is None:
            continue

        continuity_improved = row["continuity_edge_cut_ratio"] < baseline["continuity_edge_cut_ratio"]
        connector_improved = row["connector_edge_cut_ratio"] < baseline["connector_edge_cut_ratio"]
        od_not_worse = relative_not_worse(row["od_sparsity"], baseline["od_sparsity"], od_tol)
        order_cv_improved = row["order_count_cv"] < baseline["order_count_cv"]
        capacity_improved = row["capacity_violation_ratio"] < baseline["capacity_violation_ratio"]
        shape_not_worse = relative_not_worse(row["mean_elongation"], baseline["mean_elongation"], shape_tol)
        diameter_not_worse = relative_not_worse(row["mean_network_diameter_m"], baseline["mean_network_diameter_m"], shape_tol)
        structure_not_much_worse = (
            relative_not_worse(row["continuity_edge_cut_ratio"], baseline["continuity_edge_cut_ratio"], structure_tol)
            and relative_not_worse(row["connector_edge_cut_ratio"], baseline["connector_edge_cut_ratio"], structure_tol)
        )
        passes_region_growing_goal = (
            row["algorithm"] == "regularized_region_growing"
            and continuity_improved
            and connector_improved
            and od_not_worse
        )
        passes_louvain_goal = (
            row["algorithm"] == "regularized_louvain"
            and (order_cv_improved or capacity_improved)
            and structure_not_much_worse
        )
        rows.append(
            {
                "run_id": row["run_id"],
                "algorithm": row["algorithm"],
                "baseline_algorithm": baseline_name,
                "passes_region_growing_goal": passes_region_growing_goal,
                "passes_louvain_goal": passes_louvain_goal,
                "continuity_delta": row["continuity_edge_cut_ratio"] - baseline["continuity_edge_cut_ratio"],
                "connector_delta": row["connector_edge_cut_ratio"] - baseline["connector_edge_cut_ratio"],
                "od_sparsity_delta": row["od_sparsity"] - baseline["od_sparsity"],
                "order_count_cv_delta": row["order_count_cv"] - baseline["order_count_cv"],
                "capacity_violation_delta": row["capacity_violation_ratio"] - baseline["capacity_violation_ratio"],
                "shape_not_worse": shape_not_worse,
                "diameter_not_worse": diameter_not_worse,
            }
        )
    return pd.DataFrame(rows)


def run_regularized_evaluation(config_path: Path) -> None:
    config = load_evaluation_config(config_path)
    output_root = project_path(config["outputs"]["root"])
    tables_dir = output_root / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    graph_variant = str(config["scope"]["graph_variant"])
    graph = load_graph(project_path(config["inputs"]["graph"]))
    relation_edges = pd.read_csv(project_path(config["inputs"]["relation_edges"]))
    poi_features = load_optional_csv(config["inputs"].get("poi_features"))
    order_features = load_optional_csv(config["inputs"].get("order_features"))
    hourly_od = load_optional_csv(config["inputs"].get("hourly_od"))
    thresholds = metric_thresholds(config)

    metric_rows = []
    connector_type_rows = []
    for algorithm, path_value in config["inputs"]["baseline_clusters"].items():
        print(f"Computing baseline metrics for {algorithm}...")
        row, connector_rows = compute_metric_row(
            graph_variant, algorithm, baseline_params(algorithm), project_path(path_value),
            relation_edges, graph, poi_features, order_features, hourly_od, thresholds,
        )
        row.update(source_type="baseline", run_id=algorithm, initialization=algorithm, setting_id="")
        metric_rows.append(row)
        if connector_rows is not None and not connector_rows.empty:
            connector_rows = connector_rows.copy()
            connector_rows["source_type"] = "baseline"
            connector_rows["run_id"] = algorithm
            connector_type_rows.append(connector_rows)

    manifest_path = tables_dir / "run_manifest.csv"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Regularized run manifest not found: {manifest_path}. Run run_regularized_search.py first.")
    manifest = pd.read_csv(manifest_path)
    for _, manifest_row in manifest.iterrows():
        run_id = f"{manifest_row['algorithm']}_{manifest_row['setting_id']}"
        print(f"Computing regularized metrics for {run_id}...")
        row, connector_rows = compute_metric_row(
            graph_variant, str(manifest_row["algorithm"]), str(manifest_row["params"]),
            project_path(manifest_row["clusters_gpkg"]), relation_edges, graph,
            poi_features, order_features, hourly_od, thresholds,
        )
        row.update(
            source_type="regularized", run_id=run_id,
            initialization=manifest_row["initialization"], setting_id=manifest_row["setting_id"],
        )
        for column in [
            "lambda_c", "lambda_g", "lambda_r", "alpha_cont", "alpha_conn",
            "merge_split_enabled", "target_clusters", "capacity_loss", "objective",
            "r_cap", "r_graph", "r_cont", "r_conn", "r_road", "num_moves",
        ]:
            if column in manifest_row:
                row[column] = manifest_row[column]
        metric_rows.append(row)
        if connector_rows is not None and not connector_rows.empty:
            connector_rows = connector_rows.copy()
            connector_rows["source_type"] = "regularized"
            connector_rows["run_id"] = run_id
            connector_type_rows.append(connector_rows)

    metrics = pd.DataFrame(metric_rows)
    leading_columns = ["source_type", "run_id", "graph_variant", "algorithm", "initialization", "setting_id"]
    ordered_columns = [column for column in leading_columns if column in metrics.columns] + [
        column for column in metrics.columns if column not in leading_columns
    ]
    metrics = metrics.loc[:, ordered_columns]
    metrics_path = tables_dir / "metrics_regularized.csv"
    metrics.to_csv(metrics_path, index=False)
    connector_metrics = pd.concat(connector_type_rows, ignore_index=True) if connector_type_rows else pd.DataFrame()
    connector_metrics.to_csv(tables_dir / "connector_type_metrics_regularized.csv", index=False)
    build_pareto_summary(metrics, config).to_csv(tables_dir / "pareto_summary.csv", index=False)
    build_candidate_selection(metrics, config).to_csv(tables_dir / "candidate_selection.csv", index=False)
    print(f"Saved regularized metrics to {metrics_path}")
    print(f"Saved Pareto summary to {tables_dir / 'pareto_summary.csv'}")
    print(f"Saved candidate selection to {tables_dir / 'candidate_selection.csv'}")
