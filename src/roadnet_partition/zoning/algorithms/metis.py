from __future__ import annotations

import networkx as nx
import numpy as np

from roadnet_partition.zoning.algorithms.skater import configured_target_clusters


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
