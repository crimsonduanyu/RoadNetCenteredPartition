from __future__ import annotations

import importlib
import importlib.util
import sys
import types

import networkx as nx
import pytest

from roadnet_partition.zoning.algorithms.leiden import run_leiden
from roadnet_partition.zoning.algorithms.louvain import run_louvain
from roadnet_partition.zoning.algorithms.metis import run_metis
from roadnet_partition.zoning.algorithms.skater import run_skater


HAS_COMMUNITY = importlib.util.find_spec("community") is not None


@pytest.fixture(scope="module")
def legacy():
    if not HAS_COMMUNITY:
        community = types.ModuleType("community")
        community.community_louvain = types.SimpleNamespace()
        sys.modules["community"] = community
    return importlib.import_module("03_cluster_segments")


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


def test_louvain_mapping_matches_legacy(legacy, graph, config) -> None:
    if not HAS_COMMUNITY:
        pytest.skip("python-louvain is not installed")
    assert run_louvain(graph, config) == legacy.run_louvain(graph, config)


def test_skater_mapping_matches_legacy(legacy, graph, config) -> None:
    assert run_skater(graph, config) == legacy.run_skater(graph, config)


def test_leiden_mapping_matches_legacy_when_installed(legacy, graph, config) -> None:
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")
    assert run_leiden(graph, config) == legacy.run_leiden(graph, config)


def test_metis_mapping_matches_legacy_when_installed(legacy, graph, config) -> None:
    pytest.importorskip("pymetis")
    assert run_metis(graph, config) == legacy.run_metis(graph, config)
