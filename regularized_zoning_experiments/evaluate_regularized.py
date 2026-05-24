from __future__ import annotations

import json
from pathlib import Path
import pickle
import sys
from typing import Any

import geopandas as gpd
import networkx as nx
import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from utils_metric import MetricThresholds, compute_benchmark_metrics  # noqa: E402


def project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(config_path: Path) -> dict[str, Any]:
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
    with path.open("rb") as handle:
        graph = pickle.load(handle)
    if any(not isinstance(node, str) for node in graph.nodes):
        graph = nx.relabel_nodes(graph, {node: str(node) for node in graph.nodes})
    return graph


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
        if row["algorithm"] == "regularized_region_growing":
            baseline_name = "demand_region_growing"
        elif row["algorithm"] == "regularized_louvain":
            baseline_name = "louvain"
        elif row["algorithm"] == "regularized_leiden":
            baseline_name = "leiden"
        else:
            baseline_name = ""
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


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv[1:]
    config_path = project_path(argv[0]) if argv else Path(__file__).with_name("config_v1.yaml")
    config = load_config(config_path)

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
            graph_variant,
            algorithm,
            baseline_params(algorithm),
            project_path(path_value),
            relation_edges,
            graph,
            poi_features,
            order_features,
            hourly_od,
            thresholds,
        )
        row["source_type"] = "baseline"
        row["run_id"] = algorithm
        row["initialization"] = algorithm
        row["setting_id"] = ""
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
            graph_variant,
            str(manifest_row["algorithm"]),
            str(manifest_row["params"]),
            project_path(manifest_row["clusters_gpkg"]),
            relation_edges,
            graph,
            poi_features,
            order_features,
            hourly_od,
            thresholds,
        )
        row["source_type"] = "regularized"
        row["run_id"] = run_id
        row["initialization"] = manifest_row["initialization"]
        row["setting_id"] = manifest_row["setting_id"]
        for column in [
            "lambda_c",
            "lambda_g",
            "lambda_r",
            "alpha_cont",
            "alpha_conn",
            "merge_split_enabled",
            "target_clusters",
            "capacity_loss",
            "objective",
            "r_cap",
            "r_graph",
            "r_cont",
            "r_conn",
            "r_road",
            "num_moves",
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

    pareto = build_pareto_summary(metrics, config)
    pareto.to_csv(tables_dir / "pareto_summary.csv", index=False)

    selection = build_candidate_selection(metrics, config)
    selection.to_csv(tables_dir / "candidate_selection.csv", index=False)

    print(f"Saved regularized metrics to {metrics_path}")
    print(f"Saved Pareto summary to {tables_dir / 'pareto_summary.csv'}")
    print(f"Saved candidate selection to {tables_dir / 'candidate_selection.csv'}")


if __name__ == "__main__":
    main()
