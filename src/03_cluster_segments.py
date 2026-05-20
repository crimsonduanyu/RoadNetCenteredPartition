from __future__ import annotations

import pickle

import env_setup  # noqa: F401
import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from community import community_louvain
from scipy.sparse import csgraph
from scipy.sparse.linalg import eigsh
from sklearn.cluster import KMeans

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


def run_louvain(graph: nx.Graph, config: dict, target_cluster_count: int | None = None) -> dict[str, int]:
    return community_louvain.best_partition(
        graph,
        weight="weight",
        resolution=float(config["clustering"]["resolution"]),
        random_state=int(config["clustering"]["random_state"]),
    )


def run_leiden(graph: nx.Graph, config: dict, target_cluster_count: int | None = None) -> dict[str, int]:
    try:
        import igraph as ig
        import leidenalg
    except ImportError as exc:
        raise ImportError(
            "Leiden clustering requires python-igraph and leidenalg. "
            "Install them in the bj_road_partition environment."
        ) from exc

    nodes = list(graph.nodes())
    node_to_index = {node: index for index, node in enumerate(nodes)}
    edges = [(node_to_index[u], node_to_index[v]) for u, v in graph.edges()]
    weights = [float(attrs.get("weight", 1.0)) for _, _, attrs in graph.edges(data=True)]

    ig_graph = ig.Graph(n=len(nodes), edges=edges, directed=False)
    ig_graph.es["weight"] = weights
    partition = leidenalg.find_partition(
        ig_graph,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=float(config["clustering"]["resolution"]),
        seed=int(config["clustering"]["random_state"]),
    )
    memberships = partition.membership
    return {node: int(memberships[index]) for index, node in enumerate(nodes)}


def run_spectral(graph: nx.Graph, config: dict, target_cluster_count: int | None = None) -> dict[str, int]:
    if target_cluster_count is None or target_cluster_count <= 1:
        raise ValueError("Spectral clustering requires a target cluster count greater than 1.")

    nodes = list(graph.nodes())
    adjacency = nx.to_scipy_sparse_array(graph, nodelist=nodes, weight="weight", format="csr", dtype=float)
    laplacian = csgraph.laplacian(adjacency, normed=True)
    eigen_count = min(int(target_cluster_count), len(nodes) - 2)
    if eigen_count <= 1:
        raise ValueError("Graph is too small for spectral clustering.")

    _, vectors = eigsh(laplacian, k=eigen_count, which="SM")
    row_norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    embedding = np.divide(vectors, row_norms, out=np.zeros_like(vectors), where=row_norms > 0)
    labels = KMeans(
        n_clusters=int(target_cluster_count),
        n_init=10,
        random_state=int(config["clustering"]["random_state"]),
    ).fit_predict(embedding)
    return {node: int(labels[index]) for index, node in enumerate(nodes)}


ALGORITHM_RUNNERS = {
    "louvain": run_louvain,
    "leiden": run_leiden,
    "spectral": run_spectral,
}


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


def evaluate_partition(
    graph_variant: str,
    algorithm: str,
    graph: nx.Graph,
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
        "cluster_od_sparsity": cluster_od_sparsity(partition),
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


def save_partition_outputs(
    graph_variant: str,
    algorithm: str,
    base_segments: gpd.GeoDataFrame,
    partition: dict[str, int],
    config: dict,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame, pd.DataFrame]:
    label = f"{graph_variant}_{algorithm}"
    clusters_path = DATA_PROCESSED / f"segment_clusters_{label}.gpkg"
    clusters_csv_path = DATA_PROCESSED / f"segment_clusters_{label}.csv"
    summary_path = OUTPUTS_TABLES / f"cluster_summary_{label}.csv"
    diagnostics_path = OUTPUTS_TABLES / f"road_name_split_diagnostics_{label}.csv"

    segments = base_segments.copy()
    segments["cluster_id"] = segments["seg_id"].map(partition)
    segments.to_file(clusters_path, driver="GPKG")
    segments.drop(columns="geometry").to_csv(clusters_csv_path, index=False)

    summary = build_cluster_summary(segments)
    diagnostics = build_road_name_diagnostics(segments)
    summary.to_csv(summary_path, index=False)
    diagnostics.to_csv(diagnostics_path, index=False)

    default_variant = config.get("evaluation", {}).get("default_variant", "road_only")
    if graph_variant == default_variant and algorithm == "louvain":
        segments.to_file(DATA_PROCESSED / "segment_clusters.gpkg", driver="GPKG")
        segments.drop(columns="geometry").to_csv(DATA_PROCESSED / "segment_clusters.csv", index=False)
        summary.to_csv(OUTPUTS_TABLES / "cluster_summary.csv", index=False)
        diagnostics.to_csv(OUTPUTS_TABLES / "road_name_split_diagnostics.csv", index=False)

    print(f"{label} clustering completed")
    print(f"number of clusters: {segments['cluster_id'].nunique():,}")
    print(f"Saved clustered segments to {clusters_path}")
    return segments, summary, diagnostics


def main() -> None:
    ensure_directories()
    config = load_config()

    algorithms = config["clustering"].get("algorithms", [config["clustering"].get("method", "louvain")])
    unknown = [algorithm for algorithm in algorithms if algorithm not in ALGORITHM_RUNNERS]
    if unknown:
        raise ValueError(f"Unknown clustering algorithms: {unknown}. Expected one of {list(ALGORITHM_RUNNERS)}.")
    if "spectral" in algorithms and "louvain" not in algorithms:
        raise ValueError("Spectral clustering needs Louvain in clustering.algorithms to set n_clusters per graph variant.")

    nodes_path = DATA_PROCESSED / "segment_nodes.gpkg"
    print(f"Loading segment nodes from {nodes_path}...")
    base_segments = gpd.read_file(nodes_path)
    evaluation_rows = []
    louvain_cluster_counts: dict[str, int] = {}

    for graph_variant in config["semantic_graph"]["variants"]:
        graph_path = OUTPUTS_GRAPHS / f"segment_relation_graph_{graph_variant}.gpickle"
        edge_path = DATA_PROCESSED / f"segment_relation_edges_{graph_variant}.csv"

        print(f"Loading {graph_variant} graph from {graph_path}...")
        with graph_path.open("rb") as handle:
            graph = pickle.load(handle)
        edges = pd.read_csv(edge_path)

        for algorithm in algorithms:
            target_cluster_count = louvain_cluster_counts.get(graph_variant)
            if algorithm == "spectral" and target_cluster_count is None:
                raise RuntimeError(f"Cannot run spectral before Louvain for {graph_variant}.")

            print(f"Running {algorithm} on {graph_variant}...")
            partition = ALGORITHM_RUNNERS[algorithm](graph, config, target_cluster_count)
            if algorithm == "louvain":
                louvain_cluster_counts[graph_variant] = len(set(partition.values()))

            segments, summary, diagnostics = save_partition_outputs(
                graph_variant,
                algorithm,
                base_segments,
                partition,
                config,
            )
            evaluation_rows.append(
                evaluate_partition(
                    graph_variant,
                    algorithm,
                    graph,
                    edges,
                    segments,
                    partition,
                    summary,
                    diagnostics,
                )
            )

    evaluation = pd.DataFrame(evaluation_rows)
    evaluation_path = OUTPUTS_TABLES / "graph_algorithm_evaluation.csv"
    evaluation.to_csv(evaluation_path, index=False)
    comparison_path = OUTPUTS_TABLES / "comparison_evaluation.csv"
    evaluation.to_csv(comparison_path, index=False)
    ranked = build_ranked_summary(evaluation)
    ranked_path = OUTPUTS_TABLES / "graph_algorithm_ranked_summary.csv"
    ranked.to_csv(ranked_path, index=False)

    louvain_only = evaluation.loc[evaluation["algorithm"] == "louvain"].rename(columns={"graph_variant": "variant"})
    louvain_only = louvain_only.drop(columns=["algorithm"])
    louvain_only.to_csv(OUTPUTS_TABLES / "graph_variant_evaluation.csv", index=False)

    print(f"Saved graph algorithm evaluation to {evaluation_path}")
    print(f"Saved comparison evaluation to {comparison_path}")
    print(f"Saved ranked summary to {ranked_path}")


if __name__ == "__main__":
    main()
