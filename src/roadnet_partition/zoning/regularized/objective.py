from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import networkx as nx

if TYPE_CHECKING:
    from roadnet_partition.zoning.regularized.search import SearchParams

EPS = 1.0e-12

@dataclass(frozen=True)
class ObjectiveParams:
    capacity_min_ratio: float
    capacity_max_ratio: float
    target_clusters: int | None
    capacity_loss: str
    lambda_c: float
    lambda_g: float
    lambda_r: float
    alpha_cont: float
    alpha_conn: float

@dataclass
class SearchContext:
    graph: nx.Graph
    demand: dict[str, float]
    total_demand: float
    total_weight: float
    total_continuity_weight: float
    total_connector_weight: float
    objective: ObjectiveParams
    search: SearchParams

def edge_value(attrs: dict[str, Any], column: str) -> float:
    return max(float(attrs.get(column, 0.0) or 0.0), 0.0)

def build_context(
    graph: nx.Graph,
    demand: dict[str, float],
    objective: ObjectiveParams,
    search: SearchParams,
) -> SearchContext:
    total_weight = sum(edge_value(attrs, "weight") for _, _, attrs in graph.edges(data=True))
    total_continuity_weight = sum(edge_value(attrs, "continuity_weight") for _, _, attrs in graph.edges(data=True))
    total_connector_weight = sum(edge_value(attrs, "connector_weight") for _, _, attrs in graph.edges(data=True))
    return SearchContext(
        graph=graph,
        demand=demand,
        total_demand=float(sum(demand.values())),
        total_weight=max(total_weight, EPS),
        total_continuity_weight=max(total_continuity_weight, EPS),
        total_connector_weight=max(total_connector_weight, EPS),
        objective=objective,
        search=search,
    )

def target_cluster_count(context: SearchContext, cluster_demand: dict[int, float] | None = None) -> int:
    if context.objective.target_clusters is not None:
        return max(int(context.objective.target_clusters), 1)
    if cluster_demand is None:
        return max(context.graph.number_of_nodes(), 1)
    return max(len(cluster_demand), 1)

def target_demand(context: SearchContext, cluster_demand: dict[int, float] | None = None) -> float:
    return context.total_demand / target_cluster_count(context, cluster_demand)

def build_cluster_nodes(partition: dict[str, int]) -> dict[int, set[str]]:
    cluster_nodes: dict[int, set[str]] = {}
    for node, cluster_id in partition.items():
        cluster_nodes.setdefault(cluster_id, set()).add(node)
    return cluster_nodes

def build_cluster_demand(cluster_nodes: dict[int, set[str]], demand: dict[str, float]) -> dict[int, float]:
    return {cluster_id: sum(demand[node] for node in nodes) for cluster_id, nodes in cluster_nodes.items()}

def capacity_penalty_for_value(value: float, target: float, params: ObjectiveParams) -> float:
    if params.capacity_loss != "squared_hinge":
        raise ValueError(f"Unsupported capacity loss: {params.capacity_loss}")
    lower = params.capacity_min_ratio * target
    upper = params.capacity_max_ratio * target
    low_violation = max(0.0, lower - value) / max(target, EPS)
    high_violation = max(0.0, value - upper) / max(target, EPS)
    return low_violation * low_violation + high_violation * high_violation

def capacity_component(cluster_demand: dict[int, float], context: SearchContext) -> float:
    if not cluster_demand:
        return 0.0
    target = target_demand(context, cluster_demand)
    penalties = [
        capacity_penalty_for_value(value, target, context.objective)
        for value in cluster_demand.values()
    ]
    return float(sum(penalties) / len(penalties))

def objective_components(
    context: SearchContext,
    partition: dict[str, int],
    cluster_demand: dict[int, float],
) -> dict[str, float]:
    graph_cut = 0.0
    continuity_cut = 0.0
    connector_cut = 0.0
    for u, v, attrs in context.graph.edges(data=True):
        if partition[u] == partition[v]:
            continue
        graph_cut += edge_value(attrs, "weight")
        continuity_cut += edge_value(attrs, "continuity_weight")
        connector_cut += edge_value(attrs, "connector_weight")

    r_cap = capacity_component(cluster_demand, context)
    r_graph = graph_cut / context.total_weight
    r_cont = continuity_cut / context.total_continuity_weight
    r_conn = connector_cut / context.total_connector_weight
    r_road = context.objective.alpha_cont * r_cont + context.objective.alpha_conn * r_conn
    total = (
        context.objective.lambda_c * r_cap
        + context.objective.lambda_g * r_graph
        + context.objective.lambda_r * r_road
    )
    return {
        "objective": float(total),
        "r_cap": float(r_cap),
        "capacity_hinge_loss": float(r_cap),
        "r_graph": float(r_graph),
        "r_cont": float(r_cont),
        "r_conn": float(r_conn),
        "r_road": float(r_road),
    }

def move_delta(
    context: SearchContext,
    partition: dict[str, int],
    cluster_demand: dict[int, float],
    node: str,
    source: int,
    target_cluster: int,
) -> tuple[float, dict[str, float]]:
    graph_cut_delta = 0.0
    continuity_cut_delta = 0.0
    connector_cut_delta = 0.0
    for neighbor, attrs in context.graph[node].items():
        neighbor_cluster = partition[neighbor]
        before_cut = neighbor_cluster != source
        after_cut = neighbor_cluster != target_cluster
        if before_cut == after_cut:
            continue
        sign = 1.0 if after_cut else -1.0
        graph_cut_delta += sign * edge_value(attrs, "weight")
        continuity_cut_delta += sign * edge_value(attrs, "continuity_weight")
        connector_cut_delta += sign * edge_value(attrs, "connector_weight")

    cluster_count = max(len(cluster_demand), 1)
    target = target_demand(context, cluster_demand)
    node_demand = context.demand[node]
    before_capacity = (
        capacity_penalty_for_value(cluster_demand[source], target, context.objective)
        + capacity_penalty_for_value(cluster_demand[target_cluster], target, context.objective)
    ) / cluster_count
    after_capacity = (
        capacity_penalty_for_value(cluster_demand[source] - node_demand, target, context.objective)
        + capacity_penalty_for_value(cluster_demand[target_cluster] + node_demand, target, context.objective)
    ) / cluster_count
    cap_delta = after_capacity - before_capacity

    graph_delta = graph_cut_delta / context.total_weight
    cont_delta = continuity_cut_delta / context.total_continuity_weight
    conn_delta = connector_cut_delta / context.total_connector_weight
    road_delta = context.objective.alpha_cont * cont_delta + context.objective.alpha_conn * conn_delta
    total_delta = (
        context.objective.lambda_c * cap_delta
        + context.objective.lambda_g * graph_delta
        + context.objective.lambda_r * road_delta
    )
    return float(total_delta), {
        "delta_cap": float(cap_delta),
        "delta_graph": float(graph_delta),
        "delta_cont": float(cont_delta),
        "delta_conn": float(conn_delta),
        "delta_road": float(road_delta),
    }
