from __future__ import annotations

import networkx as nx
import pytest

import adaptive_clustering as legacy
from roadnet_partition.zoning.algorithms import adaptive
from roadnet_partition.zoning.algorithms.network_voronoi import run_demand_network_voronoi
from roadnet_partition.zoning.algorithms.region_growing import run_demand_region_growing


def context(graph: nx.Graph, demand: dict[str, float], target: int) -> adaptive.AdaptiveContext:
    params = adaptive.AdaptiveParams(target_clusters=target, local_search_passes=0)
    total = float(sum(demand.values()))
    return adaptive.AdaptiveContext(
        graph=graph,
        demand=demand,
        centroids={node: (float(index), 0.0) for index, node in enumerate(graph.nodes)},
        total_demand=total,
        target_demand=total / target,
        params=params,
    )


def test_definition_defaults_match_legacy() -> None:
    assert adaptive.AdaptiveParams.__dataclass_fields__.keys() == legacy.AdaptiveParams.__dataclass_fields__.keys()
    for name, field in adaptive.AdaptiveParams.__dataclass_fields__.items():
        assert field.default == legacy.AdaptiveParams.__dataclass_fields__[name].default


def test_legacy_module_is_an_explicit_identity_bridge() -> None:
    for name in [
        "adaptive_params", "allocate_component_cluster_counts",
        "select_demand_weighted_seeds", "grow_component_regions",
        "improve_boundaries", "run_demand_region_growing",
        "run_demand_network_voronoi", "relabel_partition",
    ]:
        assert getattr(legacy, name) is getattr(adaptive, name)


def test_component_budget_errors_and_isolated_node() -> None:
    graph = nx.Graph([("a", "b")])
    graph.add_node("isolated")
    demand = {node: 1.0 for node in graph}
    with pytest.raises(ValueError, match="smaller"):
        adaptive.allocate_component_cluster_counts([["a", "b"], ["isolated"]], context(graph, demand, 1))
    with pytest.raises(ValueError, match="exceeds"):
        adaptive.allocate_component_cluster_counts([["a", "b"], ["isolated"]], context(graph, demand, 4))
    assert adaptive.allocate_component_cluster_counts([["a", "b"], ["isolated"]], context(graph, demand, 2)) == [1, 1]


def test_equal_demand_seed_tie_and_non_contiguous_relabel_match_legacy() -> None:
    graph = nx.path_graph(["a", "b", "c", "d"])
    for node in graph:
        graph.nodes[node]["length"] = 1.0
    for u, v in graph.edges:
        graph[u][v]["weight"] = 1.0
    demand = {node: 1.0 for node in graph}
    new_context = context(graph, demand, 2)
    old_context = legacy.AdaptiveContext(**new_context.__dict__)
    assert adaptive.select_demand_weighted_seeds(graph, new_context, 2) == legacy.select_demand_weighted_seeds(graph, old_context, 2)
    mapping = {"a": 9, "b": 9, "c": 42}
    assert adaptive.relabel_partition(mapping) == legacy.relabel_partition(mapping) == {"a": 0, "b": 0, "c": 1}


def test_unreachable_nodes_keep_current_fallback_behavior() -> None:
    graph = nx.Graph()
    graph.add_nodes_from(["a", "b"])
    demand = {"a": 1.0, "b": 1.0}
    assignment = {"a": 0}
    remaining = {"b"}
    adaptive.assign_remaining_by_nearest_cluster(graph, context(graph, demand, 1), assignment, remaining)
    assert assignment == {"a": 0}
    assert remaining == {"b"}


def test_region_growth_and_voronoi_wrappers_use_distinct_algorithms() -> None:
    assert run_demand_region_growing is not run_demand_network_voronoi
    assert legacy.run_demand_region_growing is not legacy.run_demand_network_voronoi
