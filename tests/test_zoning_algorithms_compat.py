from __future__ import annotations

import sys
import types

import networkx as nx
import pytest

from roadnet_partition.zoning.algorithms.leiden import run_leiden
from roadnet_partition.zoning.algorithms.louvain import run_louvain
from roadnet_partition.zoning.algorithms.metis import run_metis
from roadnet_partition.zoning.algorithms.skater import run_skater

@pytest.fixture
def graph() -> nx.Graph:
    result = nx.Graph()
    result.add_node("isolated", length=2.0)
    result.add_node("a", length=1.0)
    result.add_node("b", length=2.0)
    result.add_node("c", length=3.0)
    result.add_node("d", length=4.0)
    result.add_edge("a", "b", weight=5.0)
    result.add_edge("b", "c", weight=1.0)
    result.add_edge("c", "d", weight=5.0)
    return result


@pytest.fixture
def config() -> dict:
    return {
        "clustering": {
            "resolution": 1.0,
            "random_state": 42,
            "target_clusters": 3,
            "skater": {"dissimilarity": "inverse_weight"},
            "metis": {"balance_on": "length", "edge_weight_scale": 1000},
        }
    }


def test_louvain_mapping_and_parameters(graph, config, monkeypatch) -> None:
    calls = []
    def best_partition(current_graph, **kwargs):
        calls.append(kwargs)
        return {node: index % 2 for index, node in enumerate(current_graph.nodes)}
    community = types.ModuleType("community")
    community.community_louvain = types.SimpleNamespace(best_partition=best_partition)
    monkeypatch.setitem(sys.modules, "community", community)
    expected = {"isolated": 0, "a": 1, "b": 0, "c": 1, "d": 0}
    assert run_louvain(graph, config) == expected
    assert calls == [{"weight": "weight", "resolution": 1.0, "random_state": 42}]


def test_skater_mapping_is_stable(graph, config) -> None:
    assert run_skater(graph, config) == {"a": 0, "b": 0, "c": 1, "d": 1, "isolated": 2}


def test_leiden_mapping_order_and_parameters(graph, config, monkeypatch) -> None:
    calls = []
    class EdgeSequence(dict):
        pass
    class Graph:
        def __init__(self, *, n, edges, directed):
            self.es = EdgeSequence()
            calls.append(("graph", n, edges, directed))
    igraph = types.ModuleType("igraph")
    igraph.Graph = Graph
    leidenalg = types.ModuleType("leidenalg")
    leidenalg.RBConfigurationVertexPartition = object()
    def find_partition(current_graph, partition_type, **kwargs):
        calls.append(("partition", current_graph.es["weight"], partition_type, kwargs))
        return types.SimpleNamespace(membership=[2, 0, 0, 1, 1])
    leidenalg.find_partition = find_partition
    monkeypatch.setitem(sys.modules, "igraph", igraph)
    monkeypatch.setitem(sys.modules, "leidenalg", leidenalg)
    expected = {"isolated": 2, "a": 0, "b": 0, "c": 1, "d": 1}
    assert run_leiden(graph, config) == expected
    assert calls[0] == ("graph", 5, [(1, 2), (2, 3), (3, 4)], False)
    assert calls[1][0:2] == ("partition", [5.0, 1.0, 5.0])
    assert calls[1][3] == {"weights": "weight", "resolution_parameter": 1.0, "seed": 42}


def test_metis_mapping_order_and_parameters(graph, config, monkeypatch) -> None:
    calls = []
    class CSRAdjacency:
        def __init__(self, *, adj_starts, adjacent):
            self.adj_starts = adj_starts
            self.adjacent = adjacent
    def part_graph(target, **kwargs):
        calls.append((target, kwargs))
        return types.SimpleNamespace(vertex_part=[0, 0, 1, 1, 2])
    pymetis = types.ModuleType("pymetis")
    pymetis.CSRAdjacency = CSRAdjacency
    pymetis.part_graph = part_graph
    monkeypatch.setitem(sys.modules, "pymetis", pymetis)
    expected = {"a": 0, "b": 0, "c": 1, "d": 1, "isolated": 2}
    assert run_metis(graph, config) == expected
    assert calls[0][0] == 3
    assert calls[0][1]["recursive"] is False
    assert calls[0][1]["contiguous"] is False
    assert calls[0][1]["eweights"] == [5000, 5000, 1000, 1000, 5000, 5000]
