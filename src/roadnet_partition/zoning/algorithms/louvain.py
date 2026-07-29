from __future__ import annotations

import networkx as nx


def run_louvain(graph: nx.Graph, config: dict, target_cluster_count: int | None = None) -> dict[str, int]:
    from community import community_louvain

    return community_louvain.best_partition(
        graph,
        weight="weight",
        resolution=float(config["clustering"]["resolution"]),
        random_state=int(config["clustering"]["random_state"]),
    )
