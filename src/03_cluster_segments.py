from __future__ import annotations

import pickle

import env_setup  # noqa: F401
import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from community import community_louvain

from adaptive_clustering import run_demand_network_voronoi, run_demand_region_growing
from utils_geo import ensure_scope_directories, get_scope_paths, load_config


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


def configured_target_clusters(config: dict, target_cluster_count: int | None = None) -> int:
    if target_cluster_count is not None:
        return int(target_cluster_count)
    if "target_clusters" not in config["clustering"]:
        raise ValueError("Fixed-K algorithms require clustering.target_clusters in config.yaml.")
    target = int(config["clustering"]["target_clusters"])
    if target <= 0:
        raise ValueError("clustering.target_clusters must be a positive integer.")
    return target


def allocate_component_cluster_counts(component_sizes: list[int], target_clusters: int) -> list[int]:
    component_count = len(component_sizes)
    total_nodes = sum(component_sizes)
    if target_clusters < component_count:
        raise ValueError(
            f"target_clusters={target_clusters} is smaller than the graph's "
            f"{component_count} connected components."
        )
    if target_clusters > total_nodes:
        raise ValueError(f"target_clusters={target_clusters} exceeds graph node count {total_nodes}.")

    allocations = [1] * component_count
    capacities = [size - 1 for size in component_sizes]
    remaining = target_clusters - component_count
    if remaining == 0:
        return allocations

    raw_extras = [remaining * size / total_nodes for size in component_sizes]
    floor_extras = [min(int(np.floor(raw)), capacity) for raw, capacity in zip(raw_extras, capacities)]
    allocations = [allocation + extra for allocation, extra in zip(allocations, floor_extras)]
    remaining -= sum(floor_extras)

    while remaining > 0:
        candidates = [index for index, capacity in enumerate(capacities) if allocations[index] - 1 < capacity]
        if not candidates:
            raise ValueError("Unable to allocate requested clusters across graph components.")
        candidates.sort(
            key=lambda index: (
                raw_extras[index] - floor_extras[index],
                component_sizes[index],
                -index,
            ),
            reverse=True,
        )
        selected = candidates[0]
        allocations[selected] += 1
        floor_extras[selected] += 1
        remaining -= 1

    return allocations


def edge_dissimilarity(attrs: dict, mode: str) -> float:
    weight = max(float(attrs.get("weight", 1.0)), 0.0)
    if mode == "inverse_weight":
        return float(1.0 / (weight + 1.0e-9))
    raise ValueError(f"Unknown SKATER dissimilarity mode '{mode}'.")


def run_skater(graph: nx.Graph, config: dict, target_cluster_count: int | None = None) -> dict[str, int]:
    target_clusters = configured_target_clusters(config, target_cluster_count)
    mode = config["clustering"].get("skater", {}).get("dissimilarity", "inverse_weight")
    components = [sorted(component) for component in nx.connected_components(graph)]
    components.sort(key=lambda nodes: (-len(nodes), nodes[0] if nodes else ""))
    component_allocations = allocate_component_cluster_counts([len(nodes) for nodes in components], target_clusters)

    partition: dict[str, int] = {}
    next_cluster_id = 0
    for component_nodes, component_clusters in zip(components, component_allocations):
        if component_clusters == 1:
            for node in component_nodes:
                partition[node] = next_cluster_id
            next_cluster_id += 1
            continue

        component_graph = graph.subgraph(component_nodes).copy()
        for _, _, attrs in component_graph.edges(data=True):
            attrs["dissimilarity"] = edge_dissimilarity(attrs, mode)

        tree = nx.minimum_spanning_tree(component_graph, weight="dissimilarity")
        removable_edges = sorted(
            tree.edges(data=True),
            key=lambda edge: (
                float(edge[2].get("dissimilarity", 0.0)),
                str(edge[0]),
                str(edge[1]),
            ),
            reverse=True,
        )
        for node_a, node_b, _ in removable_edges[: component_clusters - 1]:
            tree.remove_edge(node_a, node_b)

        subregions = [sorted(nodes) for nodes in nx.connected_components(tree)]
        subregions.sort(key=lambda nodes: (-len(nodes), nodes[0] if nodes else ""))
        if len(subregions) != component_clusters:
            raise RuntimeError(
                f"SKATER expected {component_clusters} clusters in a component, "
                f"but produced {len(subregions)}."
            )
        for subregion in subregions:
            for node in subregion:
                partition[node] = next_cluster_id
            next_cluster_id += 1

    return partition


def metis_vertex_weights(graph: nx.Graph, nodes: list[str], config: dict) -> list[int] | None:
    balance_on = config["clustering"].get("metis", {}).get("balance_on", "segment_count")
    if balance_on == "segment_count":
        return None
    if balance_on == "length":
        lengths = [max(float(graph.nodes[node].get("length", 1.0)), 0.0) for node in nodes]
        positive_lengths = [length for length in lengths if length > 0]
        scale = 1000.0 / float(np.median(positive_lengths)) if positive_lengths else 1.0
        return [max(1, int(round(length * scale))) for length in lengths]
    raise ValueError(f"Unknown METIS balance_on mode '{balance_on}'.")


def run_metis(graph: nx.Graph, config: dict, target_cluster_count: int | None = None) -> dict[str, int]:
    try:
        import pymetis
    except ImportError as exc:
        raise ImportError(
            "METIS clustering requires pymetis. Install/update the bj_road_partition "
            "environment from environment.yml."
        ) from exc

    target_clusters = configured_target_clusters(config, target_cluster_count)
    if target_clusters > graph.number_of_nodes():
        raise ValueError(f"target_clusters={target_clusters} exceeds graph node count {graph.number_of_nodes()}.")

    nodes = sorted(graph.nodes())
    node_to_index = {node: index for index, node in enumerate(nodes)}
    edge_weight_scale = int(config["clustering"].get("metis", {}).get("edge_weight_scale", 1000))
    if edge_weight_scale <= 0:
        raise ValueError("clustering.metis.edge_weight_scale must be positive.")

    xadj = [0]
    adjncy = []
    eweights = []
    for node in nodes:
        neighbors = sorted(graph.neighbors(node))
        for neighbor in neighbors:
            adjncy.append(node_to_index[neighbor])
            weight = max(float(graph[node][neighbor].get("weight", 1.0)), 0.0)
            eweights.append(max(1, int(round(weight * edge_weight_scale))))
        xadj.append(len(adjncy))

    adjacency = pymetis.CSRAdjacency(adj_starts=xadj, adjacent=adjncy)
    result = pymetis.part_graph(
        target_clusters,
        adjacency=adjacency,
        eweights=eweights,
        vweights=metis_vertex_weights(graph, nodes, config),
        recursive=False,
        contiguous=False,
    )
    memberships = result.vertex_part if hasattr(result, "vertex_part") else result[1]
    return {node: int(memberships[index]) for index, node in enumerate(nodes)}


ALGORITHM_RUNNERS = {
    "louvain": run_louvain,
    "leiden": run_leiden,
    "skater": run_skater,
    "metis": run_metis,
    "demand_network_voronoi": run_demand_network_voronoi,
    "demand_region_growing": run_demand_region_growing,
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


def save_partition_outputs(
    graph_variant: str,
    algorithm: str,
    base_segments: gpd.GeoDataFrame,
    partition: dict[str, int],
    config: dict,
    paths: dict,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame, pd.DataFrame]:
    label = f"{graph_variant}_{algorithm}"
    clusters_path = paths["data_processed"] / f"segment_clusters_{label}.gpkg"
    clusters_csv_path = paths["data_processed"] / f"segment_clusters_{label}.csv"
    summary_path = paths["outputs_tables"] / f"cluster_summary_{label}.csv"
    diagnostics_path = paths["outputs_tables"] / f"road_name_split_diagnostics_{label}.csv"

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
        segments.to_file(paths["data_processed"] / "segment_clusters.gpkg", driver="GPKG")
        segments.drop(columns="geometry").to_csv(paths["data_processed"] / "segment_clusters.csv", index=False)
        summary.to_csv(paths["outputs_tables"] / "cluster_summary.csv", index=False)
        diagnostics.to_csv(paths["outputs_tables"] / "road_name_split_diagnostics.csv", index=False)

    print(f"{label} clustering completed")
    print(f"number of clusters: {segments['cluster_id'].nunique():,}")
    print(f"Saved clustered segments to {clusters_path}")
    return segments, summary, diagnostics


def main() -> None:
    config = load_config()
    ensure_scope_directories(config)
    paths = get_scope_paths(config)

    algorithms = config["clustering"].get("algorithms", [config["clustering"].get("method", "louvain")])
    unknown = [algorithm for algorithm in algorithms if algorithm not in ALGORITHM_RUNNERS]
    if unknown:
        raise ValueError(f"Unknown clustering algorithms: {unknown}. Expected one of {list(ALGORITHM_RUNNERS)}.")

    nodes_path = paths["segment_nodes"]
    print(f"Loading segment nodes from {nodes_path}...")
    base_segments = gpd.read_file(nodes_path)
    evaluation_rows = []

    for graph_variant in config["semantic_graph"]["variants"]:
        graph_path = paths["outputs_graphs"] / f"segment_relation_graph_{graph_variant}.gpickle"
        edge_path = paths["data_processed"] / f"segment_relation_edges_{graph_variant}.csv"

        print(f"Loading {graph_variant} graph from {graph_path}...")
        with graph_path.open("rb") as handle:
            graph = pickle.load(handle)
        edges = pd.read_csv(edge_path)

        for algorithm in algorithms:
            print(f"Running {algorithm} on {graph_variant}...")
            partition = ALGORITHM_RUNNERS[algorithm](graph, config)

            segments, summary, diagnostics = save_partition_outputs(
                graph_variant,
                algorithm,
                base_segments,
                partition,
                config,
                paths,
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
                    paths,
                )
            )

    evaluation = pd.DataFrame(evaluation_rows)
    evaluation_path = paths["outputs_tables"] / "graph_algorithm_evaluation.csv"
    evaluation.to_csv(evaluation_path, index=False)
    comparison_path = paths["outputs_tables"] / "comparison_evaluation.csv"
    evaluation.to_csv(comparison_path, index=False)
    ranked = build_ranked_summary(evaluation)
    ranked_path = paths["outputs_tables"] / "graph_algorithm_ranked_summary.csv"
    ranked.to_csv(ranked_path, index=False)

    louvain_only = evaluation.loc[evaluation["algorithm"] == "louvain"].rename(columns={"graph_variant": "variant"})
    louvain_only = louvain_only.drop(columns=["algorithm"])
    louvain_only.to_csv(paths["outputs_tables"] / "graph_variant_evaluation.csv", index=False)

    print(f"Saved graph algorithm evaluation to {evaluation_path}")
    print(f"Saved comparison evaluation to {comparison_path}")
    print(f"Saved ranked summary to {ranked_path}")


if __name__ == "__main__":
    main()
