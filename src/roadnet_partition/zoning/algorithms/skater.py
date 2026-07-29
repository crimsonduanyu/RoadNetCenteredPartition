from __future__ import annotations

import networkx as nx

from roadnet_partition.zoning.algorithms import common


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
    """Node-count-weighted allocation (delegates to the shared clustering helper)."""
    return common.allocate_component_cluster_counts(
        weights=[float(size) for size in component_sizes],
        capacities=[size - 1 for size in component_sizes],
        target_clusters=target_clusters,
        tiebreak=lambda index: (component_sizes[index], -index),
    )


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
