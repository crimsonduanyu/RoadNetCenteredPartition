from __future__ import annotations

import pickle

import env_setup  # noqa: F401
import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from community import community_louvain

from utils_geo import DATA_PROCESSED, OUTPUTS_GRAPHS, OUTPUTS_TABLES, ensure_directories, load_config


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


def cluster_connectivity_ratio(graph, partition: dict[str, int]) -> tuple[float, float]:
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


def cluster_od_sparsity(partition: dict[str, int]) -> float:
    od_path = DATA_PROCESSED / "segment_order_od_pairs.csv"
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


def evaluate_variant(
    variant_name: str,
    graph,
    edges: pd.DataFrame,
    segments: gpd.GeoDataFrame,
    partition: dict[str, int],
    summary: pd.DataFrame,
    diagnostics: pd.DataFrame,
) -> dict:
    connected_ratio, mean_components = cluster_connectivity_ratio(graph, partition)
    total_lengths = summary["total_length_m"].astype(float)
    size_balance_cv = float(total_lengths.std(ddof=0) / total_lengths.mean()) if len(total_lengths) and total_lengths.mean() > 0 else 0.0
    named_road_splits = diagnostics["num_clusters"].astype(float) if not diagnostics.empty else pd.Series(dtype=float)

    return {
        "variant": variant_name,
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
        "cluster_od_sparsity": cluster_od_sparsity(partition),
    }


def main() -> None:
    ensure_directories()
    config = load_config()

    nodes_path = DATA_PROCESSED / "segment_nodes.gpkg"
    print(f"Loading segment nodes from {nodes_path}...")
    base_segments = gpd.read_file(nodes_path)
    evaluation_rows = []

    for variant_name in config["semantic_graph"]["variants"]:
        graph_path = OUTPUTS_GRAPHS / f"segment_relation_graph_{variant_name}.gpickle"
        edge_path = DATA_PROCESSED / f"segment_relation_edges_{variant_name}.csv"
        clusters_path = DATA_PROCESSED / f"segment_clusters_{variant_name}.gpkg"
        clusters_csv_path = DATA_PROCESSED / f"segment_clusters_{variant_name}.csv"
        summary_path = OUTPUTS_TABLES / f"cluster_summary_{variant_name}.csv"
        diagnostics_path = OUTPUTS_TABLES / f"road_name_split_diagnostics_{variant_name}.csv"

        print(f"Loading {variant_name} graph from {graph_path}...")
        with graph_path.open("rb") as handle:
            graph = pickle.load(handle)
        edges = pd.read_csv(edge_path)

        partition = community_louvain.best_partition(
            graph,
            weight="weight",
            resolution=float(config["clustering"]["resolution"]),
            random_state=int(config["clustering"]["random_state"]),
        )

        segments = base_segments.copy()
        segments["cluster_id"] = segments["seg_id"].map(partition)
        segments.to_file(clusters_path, driver="GPKG")
        segments.drop(columns="geometry").to_csv(clusters_csv_path, index=False)

        summary = build_cluster_summary(segments)
        diagnostics = build_road_name_diagnostics(segments)
        summary.to_csv(summary_path, index=False)
        diagnostics.to_csv(diagnostics_path, index=False)
        evaluation_rows.append(evaluate_variant(variant_name, graph, edges, segments, partition, summary, diagnostics))

        if variant_name == config.get("evaluation", {}).get("default_variant", "road_only"):
            segments.to_file(DATA_PROCESSED / "segment_clusters.gpkg", driver="GPKG")
            segments.drop(columns="geometry").to_csv(DATA_PROCESSED / "segment_clusters.csv", index=False)
            summary.to_csv(OUTPUTS_TABLES / "cluster_summary.csv", index=False)
            diagnostics.to_csv(OUTPUTS_TABLES / "road_name_split_diagnostics.csv", index=False)

        print(f"{variant_name} clustering completed")
        print(f"number of clusters: {segments['cluster_id'].nunique():,}")
        print("top 10 largest clusters by total road length:")
        print(summary.head(10).to_string(index=False))
        print(f"Saved clustered segments to {clusters_path}")

    evaluation = pd.DataFrame(evaluation_rows)
    evaluation_path = OUTPUTS_TABLES / "graph_variant_evaluation.csv"
    evaluation.to_csv(evaluation_path, index=False)
    print(f"Saved graph variant evaluation to {evaluation_path}")


if __name__ == "__main__":
    main()
