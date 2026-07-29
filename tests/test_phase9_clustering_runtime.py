from __future__ import annotations

import networkx as nx

from roadnet_partition.zoning.algorithms.leiden import run_leiden
from roadnet_partition.zoning.algorithms.louvain import run_louvain
from roadnet_partition.zoning.algorithms.metis import run_metis


def _graph() -> nx.Graph:
    graph = nx.Graph()
    for prefix in ("a", "b"):
        nodes = [f"{prefix}{index}" for index in range(4)]
        for left in nodes:
            for right in nodes:
                if left < right:
                    graph.add_edge(left, right, weight=5.0)
    graph.add_edge("a3", "b0", weight=0.01)
    return graph


def _config() -> dict:
    return {
        "clustering": {
            "resolution": 1.0,
            "random_state": 42,
            "target_clusters": 2,
            "metis": {"balance_on": "segment_count", "edge_weight_scale": 1000},
        }
    }


def _assert_partition(graph: nx.Graph, mapping: dict[str, int]) -> None:
    assert set(mapping) == set(graph)
    assert len(set(mapping.values())) == 2
    for cluster in set(mapping.values()):
        members = [node for node, label in mapping.items() if label == cluster]
        assert nx.is_connected(graph.subgraph(members))


def test_real_louvain_leiden_and_metis_runtime_smoke() -> None:
    graph, config = _graph(), _config()
    for runner in (run_louvain, run_leiden, run_metis):
        first = runner(graph, config, target_cluster_count=2)
        second = runner(graph, config, target_cluster_count=2)
        _assert_partition(graph, first)
        assert first == second
