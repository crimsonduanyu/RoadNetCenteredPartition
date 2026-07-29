from __future__ import annotations

import networkx as nx


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
