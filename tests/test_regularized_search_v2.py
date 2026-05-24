from __future__ import annotations

import math
from pathlib import Path
import sys

import networkx as nx
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_ROOT = PROJECT_ROOT / "regularized_zoning_experiments"
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))


@pytest.fixture(scope="module")
def search_module():
    pytest.importorskip("geopandas")
    import run_regularized_search

    return run_regularized_search


def objective_params(search_module, target_clusters: int = 3):
    return search_module.ObjectiveParams(
        capacity_min_ratio=0.5,
        capacity_max_ratio=1.5,
        target_clusters=target_clusters,
        capacity_loss="squared_hinge",
        lambda_c=1.0,
        lambda_g=0.2,
        lambda_r=0.2,
        alpha_cont=1.0,
        alpha_conn=1.0,
    )


def search_params(search_module, allow_merge_split: bool = False):
    return search_module.SearchParams(
        max_passes=2,
        min_delta=1.0e-9,
        move_policy="best_improving",
        enforce_connectivity=True,
        allow_merge_split=allow_merge_split,
        max_merge_candidates=4,
        max_merge_targets_per_cluster=3,
        max_split_candidates=4,
        split_cleanup_passes=1,
    )


def add_edge(graph: nx.Graph, u: str, v: str, weight: float = 1.0) -> None:
    graph.add_edge(
        u,
        v,
        weight=weight,
        continuity_weight=weight,
        connector_weight=weight,
    )


def test_capacity_hinge_loss_uses_fixed_target_cluster_count(search_module) -> None:
    params = objective_params(search_module, target_clusters=4)
    graph = nx.path_graph(["a", "b", "c", "d"])
    for u, v in graph.edges:
        graph[u][v].update(weight=1.0, continuity_weight=1.0, connector_weight=1.0)
    demand = {"a": 10.0, "b": 10.0, "c": 10.0, "d": 10.0}
    context = search_module.build_context(graph, demand, params, search_params(search_module))

    assert search_module.capacity_penalty_for_value(10.0, 10.0, params) == 0.0
    assert math.isclose(search_module.capacity_penalty_for_value(3.0, 10.0, params), 0.04)
    assert math.isclose(search_module.capacity_penalty_for_value(17.0, 10.0, params), 0.04)
    assert search_module.capacity_component({0: 10.0, 1: 10.0}, context) == 0.0


def test_move_delta_matches_full_objective_difference(search_module) -> None:
    graph = nx.Graph()
    add_edge(graph, "a", "b", 4.0)
    add_edge(graph, "b", "c", 1.0)
    add_edge(graph, "c", "d", 4.0)
    demand = {"a": 4.0, "b": 4.0, "c": 1.0, "d": 1.0}
    context = search_module.build_context(
        graph,
        demand,
        objective_params(search_module, target_clusters=2),
        search_params(search_module),
    )
    partition = {"a": 0, "b": 0, "c": 1, "d": 1}
    cluster_demand = search_module.build_cluster_demand(search_module.build_cluster_nodes(partition), demand)

    before = search_module.objective_components(context, partition, cluster_demand)["objective"]
    delta, _ = search_module.move_delta(context, partition, cluster_demand, "b", 0, 1)
    moved = dict(partition)
    moved["b"] = 1
    after_demand = search_module.build_cluster_demand(search_module.build_cluster_nodes(moved), demand)
    after = search_module.objective_components(context, moved, after_demand)["objective"]

    assert math.isclose(delta, after - before, rel_tol=1.0e-9, abs_tol=1.0e-9)


def test_merge_split_search_preserves_exact_k_and_connectivity(search_module) -> None:
    graph = nx.Graph()
    for node in ["a", "b", "c", "d", "e", "f"]:
        graph.add_node(node)
    add_edge(graph, "a", "b", 5.0)
    add_edge(graph, "b", "c", 5.0)
    add_edge(graph, "c", "d", 1.0)
    add_edge(graph, "d", "e", 5.0)
    add_edge(graph, "e", "f", 5.0)
    demand = {
        "a": 1.0,
        "b": 1.0,
        "c": 1.0,
        "d": 20.0,
        "e": 20.0,
        "f": 20.0,
    }
    context = search_module.build_context(
        graph,
        demand,
        objective_params(search_module, target_clusters=3),
        search_params(search_module, allow_merge_split=True),
    )
    partition = {"a": 0, "b": 1, "c": 1, "d": 2, "e": 2, "f": 2}

    result, trace, _ = search_module.run_search(context, partition, "unit", "lc1_lr1_ac1_an1_mson")
    clusters = search_module.build_cluster_nodes(result)

    assert len(clusters) == 3
    assert any(row["operation"] == "merge_split" for row in trace)
    for nodes in clusters.values():
        assert nx.is_connected(graph.subgraph(nodes))
