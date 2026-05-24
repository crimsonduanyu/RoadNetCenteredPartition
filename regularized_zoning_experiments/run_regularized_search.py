from __future__ import annotations

from dataclasses import dataclass
import itertools
import json
import math
from pathlib import Path
import pickle
import sys
from typing import Any

import geopandas as gpd
import networkx as nx
import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


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


@dataclass(frozen=True)
class SearchParams:
    max_passes: int
    min_delta: float
    move_policy: str
    enforce_connectivity: bool
    allow_merge_split: bool
    max_merge_candidates: int = 8
    max_merge_targets_per_cluster: int = 3
    max_split_candidates: int = 8
    split_cleanup_passes: int = 2


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


@dataclass(frozen=True)
class SearchSetting:
    lambda_c: float
    lambda_r: float
    alpha_cont: float
    alpha_conn: float
    merge_split_enabled: bool


@dataclass(frozen=True)
class MacroCandidate:
    merge_source: int
    merge_target: int
    split_source: int
    split_new: int
    partition: dict[str, int]
    objective: dict[str, float]
    delta: float


def project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def require_keys(config: dict[str, Any], keys: list[str], prefix: str = "config") -> None:
    for key in keys:
        if key not in config:
            raise ValueError(f"Missing required {prefix}.{key}")


def validate_config(config: dict[str, Any]) -> None:
    require_keys(config, ["scope", "inputs", "outputs", "initializations", "objective", "search", "evaluation"])
    require_keys(config["scope"], ["active", "graph_variant"], "scope")
    require_keys(config["inputs"], ["graph", "relation_edges", "segment_nodes", "order_features", "baseline_clusters"], "inputs")
    require_keys(config["outputs"], ["root"], "outputs")
    require_keys(
        config["objective"],
        ["capacity_min_ratio", "capacity_max_ratio", "lambda_g", "alpha_cont", "alpha_conn", "grid"],
        "objective",
    )
    require_keys(config["objective"]["grid"], ["lambda_c"], "objective.grid")
    require_keys(
        config["search"],
        ["max_passes", "min_delta", "move_policy", "enforce_connectivity", "allow_merge_split"],
        "search",
    )

    if config["search"]["move_policy"] != "best_improving":
        raise ValueError("Only search.move_policy='best_improving' is implemented.")
    capacity_loss = str(config["objective"].get("capacity_loss", "squared_hinge"))
    if capacity_loss != "squared_hinge":
        raise ValueError("Only objective.capacity_loss='squared_hinge' is implemented.")

    for input_key in ["graph", "relation_edges", "segment_nodes", "order_features"]:
        path = project_path(config["inputs"][input_key])
        if not path.exists():
            raise FileNotFoundError(f"Configured input does not exist: inputs.{input_key}={path}")

    baseline_clusters = config["inputs"]["baseline_clusters"]
    missing_initializations = [name for name in config["initializations"] if name not in baseline_clusters]
    if missing_initializations:
        raise ValueError(f"Initializations missing from inputs.baseline_clusters: {missing_initializations}")
    for name, path_value in baseline_clusters.items():
        path = project_path(path_value)
        if not path.exists():
            raise FileNotFoundError(f"Configured baseline cluster file does not exist: {name}={path}")

    for grid_key in ["lambda_c"]:
        values = config["objective"]["grid"].get(grid_key, [])
        if not isinstance(values, list) or not values:
            raise ValueError(f"objective.grid.{grid_key} must be a non-empty list.")

    for grid_key in ["lambda_r", "alpha_cont", "alpha_conn"]:
        values = config["objective"]["grid"].get(grid_key)
        if values is not None and (not isinstance(values, list) or not values):
            raise ValueError(f"objective.grid.{grid_key} must be a non-empty list when provided.")
    merge_split_values = config["search"].get("grid", {}).get("merge_split_enabled")
    if merge_split_values is not None and (not isinstance(merge_split_values, list) or not merge_split_values):
        raise ValueError("search.grid.merge_split_enabled must be a non-empty list when provided.")


def load_graph(path: Path) -> nx.Graph:
    with path.open("rb") as handle:
        graph = pickle.load(handle)
    if any(not isinstance(node, str) for node in graph.nodes):
        graph = nx.relabel_nodes(graph, {node: str(node) for node in graph.nodes})
    return graph


def load_demand(path: Path, graph_nodes: list[str]) -> dict[str, float]:
    features = pd.read_csv(path)
    if "seg_id" not in features.columns or "order_total" not in features.columns:
        raise ValueError(f"{path} must contain seg_id and order_total columns.")
    values = dict(
        zip(
            features["seg_id"].astype(str),
            pd.to_numeric(features["order_total"], errors="coerce").fillna(0.0),
        )
    )
    demand = {node: max(float(values.get(node, 0.0)), 0.0) for node in graph_nodes}
    if sum(demand.values()) <= EPS:
        demand = {node: 1.0 for node in graph_nodes}
    return demand


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


def relabel_partition(partition: dict[str, Any]) -> dict[str, int]:
    labels = {old: new for new, old in enumerate(sorted(set(partition.values()), key=lambda value: str(value)))}
    return {node: labels[label] for node, label in partition.items()}


def load_partition(path: Path, graph_nodes: set[str]) -> dict[str, int]:
    clusters = gpd.read_file(path)
    if "seg_id" not in clusters.columns or "cluster_id" not in clusters.columns:
        raise ValueError(f"{path} must contain seg_id and cluster_id columns.")
    partition = dict(zip(clusters["seg_id"].astype(str), clusters["cluster_id"]))
    missing = sorted(graph_nodes - set(partition))
    extra = sorted(set(partition) - graph_nodes)
    if missing:
        raise ValueError(f"{path} is missing {len(missing)} graph nodes; first missing={missing[:5]}")
    if extra:
        partition = {node: label for node, label in partition.items() if node in graph_nodes}
    return relabel_partition(partition)


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


def source_remains_connected(graph: nx.Graph, source_nodes: set[str], removed_node: str) -> bool:
    remaining = source_nodes - {removed_node}
    if len(remaining) <= 1:
        return True
    same_cluster_neighbors = [neighbor for neighbor in graph.neighbors(removed_node) if neighbor in remaining]
    if len(same_cluster_neighbors) <= 1:
        return True
    return nx.is_connected(graph.subgraph(remaining))


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


def assert_partition_valid(graph: nx.Graph, partition: dict[str, int]) -> None:
    graph_nodes = set(graph.nodes)
    partition_nodes = set(partition)
    if graph_nodes != partition_nodes:
        missing = sorted(graph_nodes - partition_nodes)
        extra = sorted(partition_nodes - graph_nodes)
        raise ValueError(f"Partition node mismatch. missing={missing[:5]}, extra={extra[:5]}")
    if any(cluster_id is None for cluster_id in partition.values()):
        raise ValueError("Partition contains null cluster labels.")


def assert_exact_target_clusters(context: SearchContext, partition: dict[str, int]) -> None:
    if context.objective.target_clusters is None:
        return
    actual = len(set(partition.values()))
    expected = int(context.objective.target_clusters)
    if actual != expected:
        raise ValueError(f"Partition has {actual} clusters but target_clusters={expected}.")


def cluster_adjacency(
    graph: nx.Graph,
    partition: dict[str, int],
    cluster_nodes: dict[int, set[str]] | None = None,
) -> dict[int, dict[int, float]]:
    adjacency: dict[int, dict[int, float]] = {
        cluster_id: {} for cluster_id in (cluster_nodes or build_cluster_nodes(partition))
    }
    for u, v, attrs in graph.edges(data=True):
        first = partition[u]
        second = partition[v]
        if first == second:
            continue
        weight = edge_value(attrs, "weight")
        adjacency.setdefault(first, {})
        adjacency.setdefault(second, {})
        adjacency[first][second] = adjacency[first].get(second, 0.0) + weight
        adjacency[second][first] = adjacency[second].get(first, 0.0) + weight
    return adjacency


def ranked_merge_sources(
    cluster_demand: dict[int, float],
    context: SearchContext,
    limit: int,
) -> list[int]:
    target = target_demand(context, cluster_demand)
    lower = context.objective.capacity_min_ratio * target
    under_capacity = [
        cluster_id
        for cluster_id, value in cluster_demand.items()
        if value < lower
    ]
    ranked = sorted(
        under_capacity,
        key=lambda cluster_id: (
            capacity_penalty_for_value(cluster_demand[cluster_id], target, context.objective),
            -cluster_demand[cluster_id],
            -cluster_id,
        ),
        reverse=True,
    )
    seen = set(ranked)
    fallback = [
        cluster_id
        for cluster_id in sorted(cluster_demand, key=lambda cid: (cluster_demand[cid], cid))
        if cluster_id not in seen
    ]
    ranked.extend(fallback)
    return ranked[: max(limit, 0)]


def ranked_split_sources(
    cluster_demand: dict[int, float],
    context: SearchContext,
    cluster_nodes: dict[int, set[str]],
    excluded: set[int],
    limit: int,
) -> list[int]:
    target = target_demand(context, cluster_demand)
    upper = context.objective.capacity_max_ratio * target
    candidates = [
        cluster_id
        for cluster_id, nodes in cluster_nodes.items()
        if cluster_id not in excluded and len(nodes) >= 2
    ]
    ranked = sorted(
        candidates,
        key=lambda cluster_id: (
            1 if cluster_demand[cluster_id] > upper else 0,
            capacity_penalty_for_value(cluster_demand[cluster_id], target, context.objective),
            cluster_demand[cluster_id],
            len(cluster_nodes[cluster_id]),
            -cluster_id,
        ),
        reverse=True,
    )
    return ranked[: max(limit, 0)]


def candidate_merge_targets(
    source: int,
    adjacency: dict[int, dict[int, float]],
    cluster_demand: dict[int, float],
    context: SearchContext,
    limit: int,
) -> list[int]:
    neighbors = adjacency.get(source, {})
    if not neighbors:
        return []
    target = target_demand(context, cluster_demand)

    def score(target_cluster: int) -> tuple[float, float, float, int]:
        before = (
            capacity_penalty_for_value(cluster_demand[source], target, context.objective)
            + capacity_penalty_for_value(cluster_demand[target_cluster], target, context.objective)
        )
        after = capacity_penalty_for_value(
            cluster_demand[source] + cluster_demand[target_cluster],
            target,
            context.objective,
        )
        return (before - after, neighbors[target_cluster], -cluster_demand[target_cluster], -target_cluster)

    return sorted(neighbors, key=score, reverse=True)[: max(limit, 0)]


def apply_merge(partition: dict[str, int], source: int, target: int) -> dict[str, int]:
    return {
        node: (target if cluster_id == source else cluster_id)
        for node, cluster_id in partition.items()
    }


def split_seed_pair(graph: nx.Graph, nodes: set[str], context: SearchContext) -> tuple[str, str] | None:
    if len(nodes) < 2:
        return None
    subgraph = graph.subgraph(nodes)
    if not nx.is_connected(subgraph):
        return None
    ordered = sorted(nodes)
    first = max(ordered, key=lambda node: (context.demand[node], subgraph.degree[node], node))
    lengths = nx.single_source_dijkstra_path_length(
        subgraph,
        first,
        weight=lambda u, v, attrs: 1.0 / (edge_value(attrs, "weight") + EPS),
    )
    second = max(
        (node for node in ordered if node != first),
        key=lambda node: (
            lengths.get(node, 0.0),
            context.demand[node],
            node,
        ),
    )
    return first, second


def split_assignment(
    graph: nx.Graph,
    nodes: set[str],
    context: SearchContext,
) -> tuple[set[str], set[str]] | None:
    seeds = split_seed_pair(graph, nodes, context)
    if seeds is None:
        return None
    seed_a, seed_b = seeds
    subgraph = graph.subgraph(nodes)
    assignment: dict[str, int] = {seed_a: 0, seed_b: 1}
    parts = {0: {seed_a}, 1: {seed_b}}
    demand = {0: context.demand[seed_a], 1: context.demand[seed_b]}
    unassigned = set(nodes) - {seed_a, seed_b}
    desired = sum(context.demand[node] for node in nodes) / 2.0

    while unassigned:
        frontier: list[tuple[int, str, float, float, str]] = []
        for side in [0, 1]:
            for part_node in parts[side]:
                for neighbor, attrs in subgraph[part_node].items():
                    if neighbor not in unassigned:
                        continue
                    new_demand = demand[side] + context.demand[neighbor]
                    balance = abs(new_demand - desired)
                    affinity = edge_value(attrs, "weight")
                    frontier.append((side, neighbor, balance, -affinity, neighbor))
        if not frontier:
            return None
        side, selected, _, _, _ = min(frontier, key=lambda item: (item[2], item[3], item[4], item[0]))
        assignment[selected] = side
        parts[side].add(selected)
        demand[side] += context.demand[selected]
        unassigned.remove(selected)

    for _ in range(max(context.search.split_cleanup_passes, 0)):
        moved = False
        part_demand = {
            0: sum(context.demand[node] for node in parts[0]),
            1: sum(context.demand[node] for node in parts[1]),
        }
        for node in sorted(nodes):
            side = assignment[node]
            other = 1 - side
            if len(parts[side]) <= 1:
                continue
            if not any(assignment.get(neighbor) == other for neighbor in subgraph.neighbors(node)):
                continue
            before = abs(part_demand[0] - desired) + abs(part_demand[1] - desired)
            node_demand = context.demand[node]
            after_values = {
                side: part_demand[side] - node_demand,
                other: part_demand[other] + node_demand,
            }
            after = abs(after_values[0] - desired) + abs(after_values[1] - desired)
            if after >= before - EPS:
                continue
            remaining = parts[side] - {node}
            if not remaining or not nx.is_connected(subgraph.subgraph(remaining)):
                continue
            parts[side].remove(node)
            parts[other].add(node)
            assignment[node] = other
            part_demand[side] -= node_demand
            part_demand[other] += node_demand
            moved = True
        if not moved:
            break

    if not parts[0] or not parts[1]:
        return None
    if not nx.is_connected(subgraph.subgraph(parts[0])) or not nx.is_connected(subgraph.subgraph(parts[1])):
        return None
    return parts[0], parts[1]


def apply_split(
    partition: dict[str, int],
    graph: nx.Graph,
    context: SearchContext,
    split_source: int,
    new_cluster_id: int,
) -> dict[str, int] | None:
    nodes = {node for node, cluster_id in partition.items() if cluster_id == split_source}
    assignment = split_assignment(graph, nodes, context)
    if assignment is None:
        return None
    keep_nodes, new_nodes = assignment
    result = dict(partition)
    for node in keep_nodes:
        result[node] = split_source
    for node in new_nodes:
        result[node] = new_cluster_id
    return result


def build_macro_candidate(
    context: SearchContext,
    partition: dict[str, int],
    current_objective: dict[str, float],
    merge_source: int,
    merge_target: int,
    split_source: int,
) -> MacroCandidate | None:
    merged = apply_merge(partition, merge_source, merge_target)
    new_cluster_id = max(set(merged.values())) + 1
    split = apply_split(merged, context.graph, context, split_source, new_cluster_id)
    if split is None:
        return None
    relabeled = relabel_partition(split)
    cluster_nodes = build_cluster_nodes(relabeled)
    if len(cluster_nodes) != len(set(partition.values())):
        return None
    if context.search.enforce_connectivity:
        for nodes in cluster_nodes.values():
            if len(nodes) > 1 and not nx.is_connected(context.graph.subgraph(nodes)):
                return None
    cluster_demand = build_cluster_demand(cluster_nodes, context.demand)
    objective = objective_components(context, relabeled, cluster_demand)
    delta = objective["objective"] - current_objective["objective"]
    if delta >= -context.search.min_delta:
        return None
    return MacroCandidate(
        merge_source=merge_source,
        merge_target=merge_target,
        split_source=split_source,
        split_new=new_cluster_id,
        partition=relabeled,
        objective=objective,
        delta=float(delta),
    )


def best_merge_only_candidate(
    context: SearchContext,
    partition: dict[str, int],
    current_objective: dict[str, float],
) -> tuple[dict[str, int], dict[str, float], float] | None:
    cluster_nodes = build_cluster_nodes(partition)
    cluster_demand = build_cluster_demand(cluster_nodes, context.demand)
    adjacency = cluster_adjacency(context.graph, partition, cluster_nodes)
    best_partition: dict[str, int] | None = None
    best_objective: dict[str, float] | None = None
    best_delta = math.inf
    for merge_source in ranked_merge_sources(cluster_demand, context, max(len(cluster_nodes), 1)):
        targets = candidate_merge_targets(
            merge_source,
            adjacency,
            cluster_demand,
            context,
            max(len(adjacency.get(merge_source, {})), 1),
        )
        for merge_target in targets:
            merged = relabel_partition(apply_merge(partition, merge_source, merge_target))
            merged_nodes = build_cluster_nodes(merged)
            if len(merged_nodes) != len(cluster_nodes) - 1:
                continue
            if context.search.enforce_connectivity:
                merged_cluster = next(
                    cluster_id
                    for cluster_id, nodes in merged_nodes.items()
                    if any(partition[node] == merge_source for node in nodes)
                    and any(partition[node] == merge_target for node in nodes)
                )
                nodes = merged_nodes[merged_cluster]
                if len(nodes) > 1 and not nx.is_connected(context.graph.subgraph(nodes)):
                    continue
            merged_demand = build_cluster_demand(merged_nodes, context.demand)
            objective = objective_components(context, merged, merged_demand)
            delta = objective["objective"] - current_objective["objective"]
            if delta < best_delta:
                best_partition = merged
                best_objective = objective
                best_delta = float(delta)
    if best_partition is None or best_objective is None:
        return None
    return best_partition, best_objective, best_delta


def best_split_only_candidate(
    context: SearchContext,
    partition: dict[str, int],
    current_objective: dict[str, float],
) -> tuple[dict[str, int], dict[str, float], float] | None:
    cluster_nodes = build_cluster_nodes(partition)
    cluster_demand = build_cluster_demand(cluster_nodes, context.demand)
    best_partition: dict[str, int] | None = None
    best_objective: dict[str, float] | None = None
    best_delta = math.inf
    for split_source in ranked_split_sources(
        cluster_demand,
        context,
        cluster_nodes,
        excluded=set(),
        limit=max(len(cluster_nodes), 1),
    ):
        split = apply_split(partition, context.graph, context, split_source, max(set(partition.values())) + 1)
        if split is None:
            continue
        relabeled = relabel_partition(split)
        split_nodes = build_cluster_nodes(relabeled)
        if len(split_nodes) != len(cluster_nodes) + 1:
            continue
        if context.search.enforce_connectivity:
            if any(len(nodes) > 1 and not nx.is_connected(context.graph.subgraph(nodes)) for nodes in split_nodes.values()):
                continue
        split_demand = build_cluster_demand(split_nodes, context.demand)
        objective = objective_components(context, relabeled, split_demand)
        delta = objective["objective"] - current_objective["objective"]
        if delta < best_delta:
            best_partition = relabeled
            best_objective = objective
            best_delta = float(delta)
    if best_partition is None or best_objective is None:
        return None
    return best_partition, best_objective, best_delta


def greedy_merge_once_to_reduce_k(context: SearchContext, partition: dict[str, int]) -> dict[str, int] | None:
    cluster_nodes = build_cluster_nodes(partition)
    cluster_demand = build_cluster_demand(cluster_nodes, context.demand)
    adjacency = cluster_adjacency(context.graph, partition, cluster_nodes)
    for merge_source in ranked_merge_sources(cluster_demand, context, max(len(cluster_nodes), 1)):
        targets = candidate_merge_targets(
            merge_source,
            adjacency,
            cluster_demand,
            context,
            max(len(adjacency.get(merge_source, {})), 1),
        )
        if not targets:
            continue
        return relabel_partition(apply_merge(partition, merge_source, targets[0]))
    return None


def normalize_partition_to_target(context: SearchContext, partition: dict[str, int]) -> dict[str, int]:
    if context.objective.target_clusters is None:
        return relabel_partition(partition)
    target = int(context.objective.target_clusters)
    current = relabel_partition(partition)
    while len(set(current.values())) != target:
        cluster_nodes = build_cluster_nodes(current)
        cluster_demand = build_cluster_demand(cluster_nodes, context.demand)
        current_objective = objective_components(context, current, cluster_demand)
        if len(cluster_nodes) > target:
            merged = greedy_merge_once_to_reduce_k(context, current)
            if merged is None:
                candidate = best_merge_only_candidate(context, current, current_objective)
                merged = candidate[0] if candidate is not None else None
            if merged is None:
                raise RuntimeError(
                    f"Unable to merge initial partition from {len(cluster_nodes)} to target_clusters={target}."
                )
            current = merged
        else:
            candidate = best_split_only_candidate(context, current, current_objective)
            if candidate is None:
                raise RuntimeError(
                    f"Unable to split initial partition from {len(cluster_nodes)} to target_clusters={target}."
                )
            current = candidate[0]
    return relabel_partition(current)


def best_merge_split_candidate(
    context: SearchContext,
    partition: dict[str, int],
    cluster_nodes: dict[int, set[str]],
    cluster_demand: dict[int, float],
    current_objective: dict[str, float],
) -> MacroCandidate | None:
    adjacency = cluster_adjacency(context.graph, partition, cluster_nodes)
    best: MacroCandidate | None = None
    for merge_source in ranked_merge_sources(cluster_demand, context, context.search.max_merge_candidates):
        for merge_target in candidate_merge_targets(
            merge_source,
            adjacency,
            cluster_demand,
            context,
            context.search.max_merge_targets_per_cluster,
        ):
            merged_cluster_id = merge_target
            excluded = {merge_source}
            split_sources = ranked_split_sources(
                cluster_demand,
                context,
                cluster_nodes,
                excluded,
                context.search.max_split_candidates,
            )
            if merged_cluster_id not in split_sources and len(cluster_nodes.get(merge_source, set())) + len(cluster_nodes.get(merge_target, set())) >= 2:
                split_sources.append(merged_cluster_id)
            for split_source in split_sources:
                candidate = build_macro_candidate(
                    context,
                    partition,
                    current_objective,
                    merge_source,
                    merge_target,
                    split_source,
                )
                if candidate is not None and (best is None or candidate.delta < best.delta):
                    best = candidate
    return best


def run_search(
    context: SearchContext,
    initial_partition: dict[str, int],
    init_name: str,
    setting_id: str,
) -> tuple[dict[str, int], list[dict[str, Any]], dict[str, float]]:
    partition = normalize_partition_to_target(context, dict(initial_partition))
    assert_partition_valid(context.graph, partition)
    assert_exact_target_clusters(context, partition)
    cluster_nodes = build_cluster_nodes(partition)
    cluster_demand = build_cluster_demand(cluster_nodes, context.demand)

    components = objective_components(context, partition, cluster_demand)
    trace: list[dict[str, Any]] = [
        {
            "initialization": init_name,
            "setting_id": setting_id,
            "step": 0,
            "pass_index": 0,
            "moved_node": "",
            "source_cluster": "",
            "target_cluster": "",
            "operation": "init",
            "merge_source": "",
            "merge_target": "",
            "split_source": "",
            "accepted_delta": 0.0,
            **components,
        }
    ]

    step = 0
    for pass_index in range(1, context.search.max_passes + 1):
        moved_this_pass = 0
        for node in sorted(context.graph.nodes):
            source = partition[node]
            if len(cluster_nodes[source]) <= 1:
                continue
            target_candidates = sorted(
                {partition[neighbor] for neighbor in context.graph.neighbors(node) if partition[neighbor] != source}
            )
            if not target_candidates:
                continue

            best_target = None
            best_delta = 0.0
            best_delta_parts: dict[str, float] = {}
            for target in target_candidates:
                delta, delta_parts = move_delta(context, partition, cluster_demand, node, source, target)
                if delta < best_delta:
                    best_delta = delta
                    best_target = target
                    best_delta_parts = delta_parts
            if best_target is None or best_delta >= -context.search.min_delta:
                continue
            if context.search.enforce_connectivity and not source_remains_connected(context.graph, cluster_nodes[source], node):
                continue

            node_demand = context.demand[node]
            cluster_nodes[source].remove(node)
            cluster_nodes[best_target].add(node)
            cluster_demand[source] -= node_demand
            cluster_demand[best_target] += node_demand
            partition[node] = best_target
            step += 1
            moved_this_pass += 1
            components = objective_components(context, partition, cluster_demand)
            if trace and components["objective"] > float(trace[-1]["objective"]) + 1.0e-8:
                raise RuntimeError(
                    f"Objective increased after accepted move in {setting_id}: "
                    f"{components['objective']} > {trace[-1]['objective']}"
                )
            trace.append(
                {
                    "initialization": init_name,
                    "setting_id": setting_id,
                    "step": step,
                    "pass_index": pass_index,
                    "moved_node": node,
                    "source_cluster": source,
                    "target_cluster": best_target,
                    "operation": "move",
                    "merge_source": "",
                    "merge_target": "",
                    "split_source": "",
                    "accepted_delta": best_delta,
                    **best_delta_parts,
                    **components,
                }
            )

        if context.search.allow_merge_split:
            candidate = best_merge_split_candidate(context, partition, cluster_nodes, cluster_demand, components)
            if candidate is not None:
                partition = candidate.partition
                assert_partition_valid(context.graph, partition)
                assert_exact_target_clusters(context, partition)
                cluster_nodes = build_cluster_nodes(partition)
                cluster_demand = build_cluster_demand(cluster_nodes, context.demand)
                components = objective_components(context, partition, cluster_demand)
                if abs(components["objective"] - candidate.objective["objective"]) > 1.0e-10:
                    raise RuntimeError(f"Macro candidate objective mismatch in {setting_id}.")
                if trace and components["objective"] > float(trace[-1]["objective"]) + 1.0e-8:
                    raise RuntimeError(
                        f"Objective increased after accepted merge/split in {setting_id}: "
                        f"{components['objective']} > {trace[-1]['objective']}"
                    )
                step += 1
                moved_this_pass += 1
                trace.append(
                    {
                        "initialization": init_name,
                        "setting_id": setting_id,
                        "step": step,
                        "pass_index": pass_index,
                        "moved_node": "",
                        "source_cluster": "",
                        "target_cluster": "",
                        "operation": "merge_split",
                        "merge_source": candidate.merge_source,
                        "merge_target": candidate.merge_target,
                        "split_source": candidate.split_source,
                        "accepted_delta": candidate.delta,
                        "delta_cap": "",
                        "delta_graph": "",
                        "delta_cont": "",
                        "delta_conn": "",
                        "delta_road": "",
                        **components,
                    }
                )
        if moved_this_pass == 0:
            break

    final_components = objective_components(context, partition, cluster_demand)
    assert_exact_target_clusters(context, partition)
    return partition, trace, final_components


def clean_setting_value(value: float | bool) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value).replace(".", "p").replace("-", "m")


def setting_id(setting: SearchSetting) -> str:
    if setting.alpha_cont == 1.0 and setting.alpha_conn == 1.0 and not setting.merge_split_enabled:
        return f"lc{clean_setting_value(setting.lambda_c)}_lr{clean_setting_value(setting.lambda_r)}"
    return (
        f"lc{clean_setting_value(setting.lambda_c)}"
        f"_lr{clean_setting_value(setting.lambda_r)}"
        f"_ac{clean_setting_value(setting.alpha_cont)}"
        f"_an{clean_setting_value(setting.alpha_conn)}"
        f"_ms{clean_setting_value(setting.merge_split_enabled)}"
    )


def build_settings(config: dict[str, Any]) -> list[SearchSetting]:
    grid = config["objective"]["grid"]
    lambda_c_values = [float(value) for value in grid["lambda_c"]]
    lambda_r_values = [float(value) for value in grid.get("lambda_r", [config["objective"].get("lambda_r", 1.0)])]
    alpha_cont_values = [float(value) for value in grid.get("alpha_cont", [config["objective"]["alpha_cont"]])]
    alpha_conn_values = [float(value) for value in grid.get("alpha_conn", [config["objective"]["alpha_conn"]])]
    merge_split_values = [
        bool(value)
        for value in config["search"].get("grid", {}).get(
            "merge_split_enabled",
            [bool(config["search"]["allow_merge_split"])],
        )
    ]
    return [
        SearchSetting(
            lambda_c=lambda_c,
            lambda_r=lambda_r,
            alpha_cont=alpha_cont,
            alpha_conn=alpha_conn,
            merge_split_enabled=merge_split_enabled,
        )
        for lambda_c, lambda_r, alpha_cont, alpha_conn, merge_split_enabled in itertools.product(
            lambda_c_values,
            lambda_r_values,
            alpha_cont_values,
            alpha_conn_values,
            merge_split_values,
        )
    ]


def legacy_setting_id(lambda_c: float, lambda_r: float) -> str:
    def clean(value: float) -> str:
        return str(value).replace(".", "p").replace("-", "m")

    return f"lc{clean(lambda_c)}_lr{clean(lambda_r)}"


def regularized_algorithm_name(initialization: str) -> str:
    if initialization == "demand_region_growing":
        return "regularized_region_growing"
    return f"regularized_{initialization}"


def save_partition(
    output_path: Path,
    csv_path: Path,
    base_segments: gpd.GeoDataFrame,
    partition: dict[str, int],
    initialization: str,
    current_setting_id: str,
    overwrite: bool,
) -> None:
    if output_path.exists() and overwrite:
        output_path.unlink()
    if csv_path.exists() and overwrite:
        csv_path.unlink()
    segments = base_segments.copy()
    segments["seg_id"] = segments["seg_id"].astype(str)
    segments["cluster_id"] = segments["seg_id"].map(partition)
    if segments["cluster_id"].isna().any():
        raise ValueError(f"Output partition is missing labels for {int(segments['cluster_id'].isna().sum())} segments.")
    segments["regularized_init"] = initialization
    segments["setting_id"] = current_setting_id
    segments.to_file(output_path, driver="GPKG")
    segments.drop(columns="geometry").to_csv(csv_path, index=False)


def write_run_config(output_root: Path, config: dict[str, Any], config_path: Path) -> None:
    copied = dict(config)
    copied["_source_config"] = str(config_path)
    with (output_root / "resolved_config.yaml").open("w", encoding="utf-8") as handle:
        yaml.safe_dump(copied, handle, sort_keys=False, allow_unicode=False)


def append_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    frame = pd.DataFrame(rows)
    frame.to_csv(path, mode="a", header=not path.exists(), index=False)


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv[1:]
    config_path = project_path(argv[0]) if argv else Path(__file__).with_name("config_v1.yaml")
    config = load_config(config_path)
    validate_config(config)

    output_root = project_path(config["outputs"]["root"])
    clusters_dir = output_root / "clusters"
    tables_dir = output_root / "tables"
    clusters_dir.mkdir(parents=True, exist_ok=True)
    tables_dir.mkdir(parents=True, exist_ok=True)
    write_run_config(output_root, config, config_path)
    manifest_path = tables_dir / "run_manifest.csv"
    trace_path = tables_dir / "objective_trace.csv"
    resume = bool(config["outputs"].get("resume", True))
    if bool(config["outputs"].get("overwrite", True)) and not resume:
        manifest_path.unlink(missing_ok=True)
        trace_path.unlink(missing_ok=True)
    completed: set[tuple[str, str]] = set()
    if resume and manifest_path.exists():
        existing_manifest = pd.read_csv(manifest_path)
        if {"algorithm", "setting_id"}.issubset(existing_manifest.columns):
            completed = {
                (str(row["algorithm"]), str(row["setting_id"]))
                for _, row in existing_manifest.iterrows()
            }

    graph = load_graph(project_path(config["inputs"]["graph"]))
    graph_nodes = sorted(str(node) for node in graph.nodes)
    demand = load_demand(project_path(config["inputs"]["order_features"]), graph_nodes)
    base_segments = gpd.read_file(project_path(config["inputs"]["segment_nodes"]))
    base_segments["seg_id"] = base_segments["seg_id"].astype(str)

    base_search_params = SearchParams(
        max_passes=int(config["search"]["max_passes"]),
        min_delta=float(config["search"]["min_delta"]),
        move_policy=str(config["search"]["move_policy"]),
        enforce_connectivity=bool(config["search"]["enforce_connectivity"]),
        allow_merge_split=bool(config["search"]["allow_merge_split"]),
        max_merge_candidates=int(config["search"].get("max_merge_candidates", 8)),
        max_merge_targets_per_cluster=int(config["search"].get("max_merge_targets_per_cluster", 3)),
        max_split_candidates=int(config["search"].get("max_split_candidates", 8)),
        split_cleanup_passes=int(config["search"].get("split_cleanup_passes", 2)),
    )
    overwrite = bool(config["outputs"].get("overwrite", True))
    graph_variant = str(config["scope"]["graph_variant"])

    settings = build_settings(config)
    baseline_clusters = config["inputs"]["baseline_clusters"]

    for initialization in config["initializations"]:
        initial_partition = load_partition(project_path(baseline_clusters[initialization]), set(graph_nodes))
        algorithm = regularized_algorithm_name(initialization)
        base_objective_params = ObjectiveParams(
            capacity_min_ratio=float(config["objective"]["capacity_min_ratio"]),
            capacity_max_ratio=float(config["objective"]["capacity_max_ratio"]),
            target_clusters=(
                int(config["objective"]["target_clusters"])
                if config["objective"].get("target_clusters") is not None
                else None
            ),
            capacity_loss=str(config["objective"].get("capacity_loss", "squared_hinge")),
            lambda_c=1.0,
            lambda_g=float(config["objective"]["lambda_g"]),
            lambda_r=float(config["objective"].get("lambda_r", 1.0)),
            alpha_cont=float(config["objective"]["alpha_cont"]),
            alpha_conn=float(config["objective"]["alpha_conn"]),
        )
        normalization_context = build_context(graph, demand, base_objective_params, base_search_params)
        normalized_initial_partition = normalize_partition_to_target(
            normalization_context,
            initial_partition,
        )
        for setting in settings:
            current_setting_id = setting_id(setting)
            output_stem = f"segment_clusters_{graph_variant}_{algorithm}_{current_setting_id}"
            gpkg_path = clusters_dir / f"{output_stem}.gpkg"
            csv_path = clusters_dir / f"{output_stem}.csv"
            if (algorithm, current_setting_id) in completed and gpkg_path.exists() and csv_path.exists():
                print(f"Skipping completed {algorithm}/{current_setting_id}...", flush=True)
                continue
            print(f"Running {algorithm}/{current_setting_id}...", flush=True)
            search_params = SearchParams(
                max_passes=base_search_params.max_passes,
                min_delta=base_search_params.min_delta,
                move_policy=base_search_params.move_policy,
                enforce_connectivity=base_search_params.enforce_connectivity,
                allow_merge_split=setting.merge_split_enabled,
                max_merge_candidates=base_search_params.max_merge_candidates,
                max_merge_targets_per_cluster=base_search_params.max_merge_targets_per_cluster,
                max_split_candidates=base_search_params.max_split_candidates,
                split_cleanup_passes=base_search_params.split_cleanup_passes,
            )
            objective_params = ObjectiveParams(
                capacity_min_ratio=float(config["objective"]["capacity_min_ratio"]),
                capacity_max_ratio=float(config["objective"]["capacity_max_ratio"]),
                target_clusters=(
                    int(config["objective"]["target_clusters"])
                    if config["objective"].get("target_clusters") is not None
                    else None
                ),
                capacity_loss=str(config["objective"].get("capacity_loss", "squared_hinge")),
                lambda_c=setting.lambda_c,
                lambda_g=float(config["objective"]["lambda_g"]),
                lambda_r=setting.lambda_r,
                alpha_cont=setting.alpha_cont,
                alpha_conn=setting.alpha_conn,
            )
            context = build_context(graph, demand, objective_params, search_params)
            partition, trace, final_components = run_search(context, normalized_initial_partition, initialization, current_setting_id)

            save_partition(gpkg_path, csv_path, base_segments, partition, initialization, current_setting_id, overwrite)

            params = {
                "initialization": initialization,
                "setting_id": current_setting_id,
                "capacity_min_ratio": objective_params.capacity_min_ratio,
                "capacity_max_ratio": objective_params.capacity_max_ratio,
                "target_clusters": objective_params.target_clusters,
                "capacity_loss": objective_params.capacity_loss,
                "lambda_c": objective_params.lambda_c,
                "lambda_g": objective_params.lambda_g,
                "lambda_r": objective_params.lambda_r,
                "alpha_cont": objective_params.alpha_cont,
                "alpha_conn": objective_params.alpha_conn,
                "max_passes": search_params.max_passes,
                "min_delta": search_params.min_delta,
                "move_policy": search_params.move_policy,
                "enforce_connectivity": search_params.enforce_connectivity,
                "allow_merge_split": search_params.allow_merge_split,
                "max_merge_candidates": search_params.max_merge_candidates,
                "max_merge_targets_per_cluster": search_params.max_merge_targets_per_cluster,
                "max_split_candidates": search_params.max_split_candidates,
                "split_cleanup_passes": search_params.split_cleanup_passes,
            }
            manifest_row = {
                "graph_variant": graph_variant,
                "algorithm": algorithm,
                "initialization": initialization,
                "setting_id": current_setting_id,
                "lambda_c": objective_params.lambda_c,
                "lambda_g": objective_params.lambda_g,
                "lambda_r": objective_params.lambda_r,
                "alpha_cont": objective_params.alpha_cont,
                "alpha_conn": objective_params.alpha_conn,
                "merge_split_enabled": search_params.allow_merge_split,
                "target_clusters": objective_params.target_clusters,
                "capacity_loss": objective_params.capacity_loss,
                "num_clusters": len(set(partition.values())),
                "num_moves": max(len(trace) - 1, 0),
                "clusters_gpkg": str(gpkg_path.relative_to(PROJECT_ROOT)),
                "clusters_csv": str(csv_path.relative_to(PROJECT_ROOT)),
                "params": json.dumps(params, sort_keys=True),
                **final_components,
            }
            append_rows(manifest_path, [manifest_row])
            append_rows(trace_path, trace)
            completed.add((algorithm, current_setting_id))

    if manifest_path.exists():
        manifest = pd.read_csv(manifest_path)
        manifest = manifest.drop_duplicates(["algorithm", "setting_id"], keep="last").sort_values(
            ["initialization", "lambda_c", "lambda_r", "alpha_cont", "alpha_conn", "merge_split_enabled"]
        ).reset_index(drop=True)
        manifest.to_csv(manifest_path, index=False)
    print(f"Saved manifest to {manifest_path}")
    print(f"Saved objective trace to {trace_path}")


if __name__ == "__main__":
    main()
