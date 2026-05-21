from __future__ import annotations

import json
import pickle

import env_setup  # noqa: F401
import geopandas as gpd
import pandas as pd

from utils_geo import ensure_scope_directories, get_scope_paths, load_config
from utils_metric import MetricThresholds, compute_benchmark_metrics, load_or_build_hourly_segment_od


CORE_PREVIEW_COLUMNS = [
    "graph_variant",
    "algorithm",
    "num_clusters",
    "connected_cluster_ratio",
    "connector_edge_cut_ratio",
    "continuity_edge_cut_ratio",
    "weighted_mean_dominant_cluster_share",
    "mean_compactness",
    "road_length_cv",
    "poi_wss_per_segment",
    "order_wss_per_segment",
    "od_sparsity",
    "historical_avg_wape",
]


def load_optional_csv(path) -> pd.DataFrame | None:
    if not path.exists():
        print(f"Optional metric input not found: {path}")
        return None
    return pd.read_csv(path)


def clustering_params(config: dict, algorithm: str) -> str:
    params: dict[str, object] = {"random_state": config["clustering"].get("random_state")}
    if algorithm in {"louvain", "leiden"}:
        params["resolution"] = config["clustering"].get("resolution")
    if algorithm == "spectral":
        params["target_cluster_count"] = "louvain_same_variant"
    return json.dumps(params, ensure_ascii=False, sort_keys=True)


def main() -> None:
    config = load_config()
    ensure_scope_directories(config)
    paths = get_scope_paths(config)

    poi_features = load_optional_csv(paths["poi_features"])
    order_features = load_optional_csv(paths["order_features"])
    hourly_od = load_or_build_hourly_segment_od(config, paths)
    thresholds = MetricThresholds()

    metric_rows = []
    connector_type_rows = []
    algorithms = config["clustering"].get("algorithms", [config["clustering"].get("method", "louvain")])

    for graph_variant in config["semantic_graph"]["variants"]:
        graph_path = paths["outputs_graphs"] / f"segment_relation_graph_{graph_variant}.gpickle"
        edge_path = paths["data_processed"] / f"segment_relation_edges_{graph_variant}.csv"
        if not graph_path.exists() or not edge_path.exists():
            print(f"Skipping {graph_variant}: missing graph or relation edge file.")
            continue

        with graph_path.open("rb") as handle:
            graph = pickle.load(handle)
        relation_edges = pd.read_csv(edge_path)

        for algorithm in algorithms:
            clusters_path = paths["data_processed"] / f"segment_clusters_{graph_variant}_{algorithm}.gpkg"
            if not clusters_path.exists():
                print(f"Skipping {graph_variant}/{algorithm}: missing cluster file {clusters_path}")
                continue

            print(f"Computing benchmark metrics for {graph_variant}/{algorithm}...")
            clusters = gpd.read_file(clusters_path)
            row, connector_rows = compute_benchmark_metrics(
                graph_variant=graph_variant,
                algorithm=algorithm,
                params=clustering_params(config, algorithm),
                clusters=clusters,
                relation_edges=relation_edges,
                graph=graph,
                poi_features=poi_features,
                order_features=order_features,
                hourly_od=hourly_od,
                thresholds=thresholds,
            )
            metric_rows.append(row)
            if connector_rows is not None and not connector_rows.empty:
                connector_type_rows.append(connector_rows)

    if not metric_rows:
        raise RuntimeError("No cluster benchmark metrics were computed. Run 03_cluster_segments.py first.")

    metrics = pd.DataFrame(metric_rows).sort_values(["graph_variant", "algorithm"]).reset_index(drop=True)
    metrics_path = paths["outputs_tables"] / "benchmark_metrics.csv"
    metrics.to_csv(metrics_path, index=False)

    if connector_type_rows:
        connector_metrics = pd.concat(connector_type_rows, ignore_index=True)
        connector_metrics = connector_metrics.sort_values(["graph_variant", "algorithm", "connector_type"])
    else:
        connector_metrics = pd.DataFrame(
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
    connector_path = paths["outputs_tables"] / "benchmark_connector_type_metrics.csv"
    connector_metrics.to_csv(connector_path, index=False)

    default_variant = config.get("evaluation", {}).get("default_variant", "road_only")
    default = metrics.loc[(metrics["graph_variant"] == default_variant) & (metrics["algorithm"] == "louvain")].copy()
    default_path = paths["outputs_tables"] / "benchmark_metrics_default.csv"
    default.to_csv(default_path, index=False)

    print(f"Saved benchmark metrics to {metrics_path}")
    print(f"Saved connector type metrics to {connector_path}")
    print(f"Saved default benchmark metrics to {default_path}")
    preview_columns = [column for column in CORE_PREVIEW_COLUMNS if column in metrics.columns]
    print("\nBenchmark metric preview:")
    print(metrics[preview_columns].to_string(index=False))


if __name__ == "__main__":
    main()
