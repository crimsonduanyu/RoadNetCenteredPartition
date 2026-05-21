from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
from pathlib import Path
from typing import Any

import env_setup  # noqa: F401
import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd

from utils_geo import get_scope_paths


EPS = 1.0e-9


@dataclass(frozen=True)
class AdaptiveParams:
    target_clusters: int
    seed_theta: float = 0.75
    capacity_min_ratio: float = 0.5
    capacity_max_ratio: float = 1.5
    edge_cost_epsilon: float = 1.0e-6
    alpha_conn: float = 1.0
    beta_poi: float = 0.5
    gamma_order: float = 0.5
    eta_continuity: float = 1.0
    lambda_capacity: float = 1.0
    lambda_shape: float = 0.2
    shape_distance_scale_m: float = 3000.0
    local_search_passes: int = 2
    local_search_tolerance: float = 1.0e-9
    lambda_cut: float = 1.0
    lambda_semantic_cut: float = 0.5
    lambda_local_capacity: float = 1.0


@dataclass
class AdaptiveContext:
    graph: nx.Graph
    demand: dict[str, float]
    centroids: dict[str, tuple[float, float]]
    total_demand: float
    target_demand: float
    params: AdaptiveParams


def adaptive_params(config: dict[str, Any], target_cluster_count: int | None = None) -> AdaptiveParams:
    clustering = config["clustering"]
    adaptive = clustering.get("adaptive", {})
    target = int(target_cluster_count or clustering.get("target_clusters"))
    if target <= 0:
        raise ValueError("clustering.target_clusters must be a positive integer.")
    return AdaptiveParams(
        target_clusters=target,
        seed_theta=float(adaptive.get("seed_theta", 0.75)),
        capacity_min_ratio=float(adaptive.get("capacity_min_ratio", 0.5)),
        capacity_max_ratio=float(adaptive.get("capacity_max_ratio", 1.5)),
        edge_cost_epsilon=float(adaptive.get("edge_cost_epsilon", 1.0e-6)),
        alpha_conn=float(adaptive.get("alpha_conn", 1.0)),
        beta_poi=float(adaptive.get("beta_poi", 0.5)),
        gamma_order=float(adaptive.get("gamma_order", 0.5)),
        eta_continuity=float(adaptive.get("eta_continuity", 1.0)),
        lambda_capacity=float(adaptive.get("lambda_capacity", 1.0)),
        lambda_shape=float(adaptive.get("lambda_shape", 0.2)),
        shape_distance_scale_m=float(adaptive.get("shape_distance_scale_m", 3000.0)),
        local_search_passes=int(adaptive.get("local_search_passes", 2)),
        local_search_tolerance=float(adaptive.get("local_search_tolerance", 1.0e-9)),
        lambda_cut=float(adaptive.get("lambda_cut", 1.0)),
        lambda_semantic_cut=float(adaptive.get("lambda_semantic_cut", 0.5)),
        lambda_local_capacity=float(adaptive.get("lambda_local_capacity", 1.0)),
    )


def load_context(graph: nx.Graph, config: dict[str, Any], target_cluster_count: int | None = None) -> AdaptiveContext:
    paths = get_scope_paths(config)
    params = adaptive_params(config, target_cluster_count)
    demand = load_segment_demand(paths["order_features"], graph.nodes)
    centroids = load_segment_centroids(paths["segment_nodes"], graph.nodes)
    total_demand = float(sum(demand.values()))
    if total_demand <= 0:
        total_demand = float(graph.number_of_nodes())
        demand = {node: 1.0 for node in graph.nodes}
    target_demand = total_demand / params.target_clusters
    return AdaptiveContext(
        graph=graph,
        demand=demand,
        centroids=centroids,
        total_demand=total_demand,
        target_demand=target_demand,
        params=params,
    )


def load_segment_demand(path: Path, nodes) -> dict[str, float]:
    node_ids = [str(node) for node in nodes]
    if not path.exists():
        return {node: 0.0 for node in node_ids}
    features = pd.read_csv(path, usecols=["seg_id", "order_total"])
    values = dict(zip(features["seg_id"].astype(str), pd.to_numeric(features["order_total"], errors="coerce").fillna(0.0)))
    return {node: max(float(values.get(str(node), 0.0)), 0.0) for node in node_ids}


def load_segment_centroids(path: Path, nodes) -> dict[str, tuple[float, float]]:
    node_set = {str(node) for node in nodes}
    segments = gpd.read_file(path, columns=["seg_id", "geometry"])
    segments["seg_id"] = segments["seg_id"].astype(str)
    segments = segments.loc[segments["seg_id"].isin(node_set)].copy()
    centroids = segments.geometry.centroid
    return {
        str(seg_id): (float(point.x), float(point.y))
        for seg_id, point in zip(segments["seg_id"], centroids)
        if point is not None and not point.is_empty
    }


def edge_cost(context: AdaptiveContext):
    def cost(u: str, v: str, attrs: dict[str, Any]) -> float:
        length_u = max(float(context.graph.nodes[u].get("length", 1.0) or 1.0), 1.0)
        length_v = max(float(context.graph.nodes[v].get("length", 1.0) or 1.0), 1.0)
        weight = max(float(attrs.get("weight", 1.0) or 1.0), 0.0)
        return ((length_u + length_v) / 2.0) / (weight + context.params.edge_cost_epsilon)

    return cost


def allocate_component_cluster_counts(
    components: list[list[str]],
    context: AdaptiveContext,
) -> list[int]:
    target_clusters = context.params.target_clusters
    if target_clusters < len(components):
        raise ValueError(
            f"target_clusters={target_clusters} is smaller than the graph's "
            f"{len(components)} connected components."
        )
    total_nodes = sum(len(component) for component in components)
    if target_clusters > total_nodes:
        raise ValueError(f"target_clusters={target_clusters} exceeds graph node count {total_nodes}.")

    component_demands = [sum(context.demand[node] for node in component) for component in components]
    if sum(component_demands) <= 0:
        component_demands = [float(len(component)) for component in components]

    allocations = [1] * len(components)
    capacities = [len(component) - 1 for component in components]
    remaining = target_clusters - len(components)
    if remaining == 0:
        return allocations

    total_weight = float(sum(component_demands))
    raw_extras = [remaining * demand / total_weight for demand in component_demands]
    floor_extras = [min(int(math.floor(raw)), capacity) for raw, capacity in zip(raw_extras, capacities)]
    allocations = [allocation + extra for allocation, extra in zip(allocations, floor_extras)]
    remaining -= sum(floor_extras)

    while remaining > 0:
        candidates = [index for index, capacity in enumerate(capacities) if allocations[index] - 1 < capacity]
        if not candidates:
            raise ValueError("Unable to allocate requested clusters across graph components.")
        candidates.sort(
            key=lambda index: (
                raw_extras[index] - floor_extras[index],
                component_demands[index],
                len(components[index]),
                -index,
            ),
            reverse=True,
        )
        selected = candidates[0]
        allocations[selected] += 1
        floor_extras[selected] += 1
        remaining -= 1

    return allocations


def demand_signal(context: AdaptiveContext, nodes: list[str]) -> dict[str, float]:
    raw = {node: math.log1p(context.demand[node]) for node in nodes}
    positive = [value for value in raw.values() if value > 0]
    floor = 0.05 * float(np.median(positive)) if positive else 1.0
    floor = max(floor, 1.0e-3)
    return {node: max(value, floor) for node, value in raw.items()}


def select_demand_weighted_seeds(component_graph: nx.Graph, context: AdaptiveContext, k: int) -> list[str]:
    nodes = sorted(component_graph.nodes)
    if k <= 0:
        return []
    if k >= len(nodes):
        return nodes

    signal = demand_signal(context, nodes)
    first = max(nodes, key=lambda node: (signal[node], context.demand[node], component_graph.degree[node], node))
    seeds = [first]
    min_dist = {node: math.inf for node in nodes}
    weight = edge_cost(context)

    for _ in range(1, k):
        lengths = nx.single_source_dijkstra_path_length(component_graph, seeds[-1], weight=weight)
        for node, distance in lengths.items():
            if distance < min_dist[node]:
                min_dist[node] = float(distance)
        next_seed = max(
            (node for node in nodes if node not in seeds),
            key=lambda node: (
                (signal[node] ** context.params.seed_theta) * (min_dist[node] if math.isfinite(min_dist[node]) else 0.0),
                signal[node],
                node,
            ),
        )
        seeds.append(next_seed)

    return seeds


def multisource_assign(component_graph: nx.Graph, context: AdaptiveContext, seeds: list[str]) -> dict[str, int]:
    heap: list[tuple[float, int, str]] = []
    assignment: dict[str, int] = {}
    best_distance: dict[str, float] = {}
    weight = edge_cost(context)

    for seed_index, seed in enumerate(seeds):
        heapq.heappush(heap, (0.0, seed_index, seed))

    while heap:
        distance, seed_index, node = heapq.heappop(heap)
        if node in best_distance:
            continue
        best_distance[node] = distance
        assignment[node] = seed_index
        for neighbor, attrs in component_graph[node].items():
            if neighbor in best_distance:
                continue
            heapq.heappush(heap, (distance + weight(node, neighbor, attrs), seed_index, neighbor))

    return assignment


def capacitated_multisource_assign(component_graph: nx.Graph, context: AdaptiveContext, seeds: list[str]) -> dict[str, int]:
    assignment: dict[str, int] = {seed: seed_index for seed_index, seed in enumerate(seeds)}
    unassigned = set(component_graph.nodes) - set(seeds)
    cluster_demand = {seed_index: context.demand[seed] for seed_index, seed in enumerate(seeds)}
    weight = edge_cost(context)
    heap: list[tuple[float, int, str]] = []

    def push_frontier_from(node: str, cluster_id: int, base_distance: float = 0.0) -> None:
        for neighbor, attrs in component_graph[node].items():
            if neighbor not in assignment:
                heapq.heappush(heap, (base_distance + weight(node, neighbor, attrs), cluster_id, neighbor))

    for seed_index, seed in enumerate(seeds):
        push_frontier_from(seed, seed_index)

    relaxed_capacity = max(context.params.capacity_max_ratio * context.target_demand, EPS)
    skipped: list[tuple[float, int, str]] = []

    while unassigned:
        if not heap:
            if skipped:
                relaxed_capacity *= 1.25
                heap, skipped = skipped, []
                heapq.heapify(heap)
                continue
            assign_remaining_by_nearest_cluster(component_graph, context, assignment, unassigned)
            break

        distance, cluster_id, node = heapq.heappop(heap)
        if node in assignment:
            continue

        demand_after = cluster_demand[cluster_id] + context.demand[node]
        has_room_elsewhere = any(value < relaxed_capacity for value in cluster_demand.values())
        if demand_after > relaxed_capacity and has_room_elsewhere:
            skipped.append((distance, cluster_id, node))
            continue

        assignment[node] = cluster_id
        unassigned.remove(node)
        cluster_demand[cluster_id] = demand_after
        push_frontier_from(node, cluster_id, distance)

    return assignment


def run_demand_network_voronoi(
    graph: nx.Graph,
    config: dict[str, Any],
    target_cluster_count: int | None = None,
) -> dict[str, int]:
    context = load_context(graph, config, target_cluster_count)
    components = sorted((sorted(component) for component in nx.connected_components(graph)), key=lambda nodes: (-len(nodes), nodes[0]))
    allocations = allocate_component_cluster_counts(components, context)

    partition: dict[str, int] = {}
    next_cluster_id = 0
    for component_nodes, component_clusters in zip(components, allocations):
        component_graph = graph.subgraph(component_nodes).copy()
        seeds = select_demand_weighted_seeds(component_graph, context, component_clusters)
        local_assignment = capacitated_multisource_assign(component_graph, context, seeds)
        for node, local_cluster in local_assignment.items():
            partition[node] = next_cluster_id + local_cluster
        next_cluster_id += component_clusters

    return improve_boundaries(graph, partition, context)


def run_demand_region_growing(
    graph: nx.Graph,
    config: dict[str, Any],
    target_cluster_count: int | None = None,
) -> dict[str, int]:
    context = load_context(graph, config, target_cluster_count)
    components = sorted((sorted(component) for component in nx.connected_components(graph)), key=lambda nodes: (-len(nodes), nodes[0]))
    allocations = allocate_component_cluster_counts(components, context)

    partition: dict[str, int] = {}
    next_cluster_id = 0
    for component_nodes, component_clusters in zip(components, allocations):
        component_graph = graph.subgraph(component_nodes).copy()
        seeds = select_demand_weighted_seeds(component_graph, context, component_clusters)
        local_partition = grow_component_regions(component_graph, context, seeds)
        for node, local_cluster in local_partition.items():
            partition[node] = next_cluster_id + local_cluster
        next_cluster_id += component_clusters

    return improve_boundaries(graph, partition, context)


def grow_component_regions(component_graph: nx.Graph, context: AdaptiveContext, seeds: list[str]) -> dict[str, int]:
    local_partition: dict[str, int] = {seed: index for index, seed in enumerate(seeds)}
    unassigned = set(component_graph.nodes) - set(seeds)
    cluster_nodes: dict[int, set[str]] = {index: {seed} for index, seed in enumerate(seeds)}
    cluster_demand = {index: context.demand[seed] for index, seed in enumerate(seeds)}
    cluster_centroid = {index: context.centroids.get(seed, (0.0, 0.0)) for index, seed in enumerate(seeds)}
    active = set(range(len(seeds)))

    while unassigned:
        if not active:
            assign_remaining_by_nearest_cluster(component_graph, context, local_partition, unassigned)
            break

        cluster_id = min(active, key=lambda cid: (cluster_demand[cid] / (context.target_demand + EPS), cid))
        candidates = {
            neighbor
            for node in cluster_nodes[cluster_id]
            for neighbor in component_graph.neighbors(node)
            if neighbor in unassigned
        }
        if not candidates:
            active.discard(cluster_id)
            continue

        best_node = max(
            candidates,
            key=lambda node: region_candidate_score(
                component_graph,
                context,
                node,
                cluster_id,
                local_partition,
                cluster_demand,
                cluster_centroid,
            ),
        )
        local_partition[best_node] = cluster_id
        unassigned.remove(best_node)
        cluster_nodes[cluster_id].add(best_node)
        old_demand = cluster_demand[cluster_id]
        node_demand = context.demand[best_node]
        cluster_demand[cluster_id] = old_demand + node_demand
        cluster_centroid[cluster_id] = update_centroid(
            cluster_centroid[cluster_id],
            old_demand,
            context.centroids.get(best_node, cluster_centroid[cluster_id]),
            max(node_demand, 1.0),
        )
        active.add(cluster_id)
        for neighbor in component_graph.neighbors(best_node):
            neighbor_cluster = local_partition.get(neighbor)
            if neighbor_cluster is not None:
                active.add(neighbor_cluster)

    return local_partition


def assign_remaining_by_nearest_cluster(
    graph: nx.Graph,
    context: AdaptiveContext,
    partition: dict[str, int],
    unassigned: set[str],
) -> None:
    heap: list[tuple[float, int, str]] = []
    weight = edge_cost(context)
    for node, cluster_id in partition.items():
        heapq.heappush(heap, (0.0, cluster_id, node))

    while heap and unassigned:
        distance, cluster_id, node = heapq.heappop(heap)
        for neighbor, attrs in graph[node].items():
            if neighbor in partition:
                continue
            if neighbor in unassigned:
                partition[neighbor] = cluster_id
                unassigned.remove(neighbor)
                heapq.heappush(heap, (distance + weight(node, neighbor, attrs), cluster_id, neighbor))


def update_centroid(
    old_centroid: tuple[float, float],
    old_weight: float,
    new_point: tuple[float, float],
    new_weight: float,
) -> tuple[float, float]:
    total = max(old_weight, 1.0) + max(new_weight, 1.0)
    return (
        (old_centroid[0] * max(old_weight, 1.0) + new_point[0] * max(new_weight, 1.0)) / total,
        (old_centroid[1] * max(old_weight, 1.0) + new_point[1] * max(new_weight, 1.0)) / total,
    )


def region_candidate_score(
    graph: nx.Graph,
    context: AdaptiveContext,
    node: str,
    cluster_id: int,
    partition: dict[str, int],
    cluster_demand: dict[int, float],
    cluster_centroid: dict[int, tuple[float, float]],
) -> float:
    adjacent_edges = [(neighbor, graph[node][neighbor]) for neighbor in graph.neighbors(node)]
    total_weight = sum(max(float(attrs.get("weight", 0.0) or 0.0), 0.0) for _, attrs in adjacent_edges) + EPS
    internal_edges = [(neighbor, attrs) for neighbor, attrs in adjacent_edges if partition.get(neighbor) == cluster_id]
    internal_weight = sum(max(float(attrs.get("weight", 0.0) or 0.0), 0.0) for _, attrs in internal_edges)
    conn = internal_weight / total_weight
    continuity = sum(max(float(attrs.get("continuity_weight", 0.0) or 0.0), 0.0) for _, attrs in internal_edges) / total_weight
    poi = sum(max(float(attrs.get("poi_similarity", 0.0) or 0.0), 0.0) for _, attrs in internal_edges) / max(len(internal_edges), 1)
    order = sum(max(float(attrs.get("order_similarity", 0.0) or 0.0), 0.0) for _, attrs in internal_edges) / max(len(internal_edges), 1)

    new_demand = cluster_demand[cluster_id] + context.demand[node]
    capacity_penalty = abs(new_demand - context.target_demand) / (context.target_demand + EPS)
    if cluster_demand[cluster_id] < context.params.capacity_min_ratio * context.target_demand:
        capacity_penalty *= 0.5
    elif new_demand > context.params.capacity_max_ratio * context.target_demand:
        capacity_penalty *= 1.5

    shape_penalty = 0.0
    if node in context.centroids and cluster_id in cluster_centroid:
        cx, cy = cluster_centroid[cluster_id]
        nx_, ny_ = context.centroids[node]
        shape_penalty = math.dist((cx, cy), (nx_, ny_)) / max(context.params.shape_distance_scale_m, EPS)

    return (
        context.params.alpha_conn * conn
        + context.params.eta_continuity * continuity
        + context.params.beta_poi * poi
        + context.params.gamma_order * order
        - context.params.lambda_capacity * capacity_penalty
        - context.params.lambda_shape * shape_penalty
    )


def improve_boundaries(graph: nx.Graph, partition: dict[str, int], context: AdaptiveContext) -> dict[str, int]:
    cluster_nodes = build_cluster_nodes(partition)
    cluster_demand = {
        cluster_id: sum(context.demand[node] for node in nodes)
        for cluster_id, nodes in cluster_nodes.items()
    }

    for _ in range(max(context.params.local_search_passes, 0)):
        moved = False
        for node in sorted(graph.nodes):
            source = partition[node]
            target_candidates = sorted({partition[neighbor] for neighbor in graph.neighbors(node) if partition[neighbor] != source})
            if not target_candidates or len(cluster_nodes[source]) <= 1:
                continue

            best_target = None
            best_delta = 0.0
            for target in target_candidates:
                delta = move_objective_delta(graph, context, partition, cluster_demand, node, source, target)
                if delta < best_delta:
                    best_delta = delta
                    best_target = target

            if best_target is None or best_delta >= -context.params.local_search_tolerance:
                continue
            if not source_remains_connected(graph, cluster_nodes[source], node):
                continue

            node_demand = context.demand[node]
            cluster_nodes[source].remove(node)
            cluster_nodes[best_target].add(node)
            cluster_demand[source] -= node_demand
            cluster_demand[best_target] += node_demand
            partition[node] = best_target
            moved = True

        if not moved:
            break

    return relabel_partition(partition)


def build_cluster_nodes(partition: dict[str, int]) -> dict[int, set[str]]:
    cluster_nodes: dict[int, set[str]] = {}
    for node, cluster_id in partition.items():
        cluster_nodes.setdefault(cluster_id, set()).add(node)
    return cluster_nodes


def source_remains_connected(graph: nx.Graph, source_nodes: set[str], removed_node: str) -> bool:
    remaining = source_nodes - {removed_node}
    if len(remaining) <= 1:
        return True
    same_cluster_neighbors = [neighbor for neighbor in graph.neighbors(removed_node) if neighbor in remaining]
    if len(same_cluster_neighbors) <= 1:
        return True
    return nx.is_connected(graph.subgraph(remaining))


def move_objective_delta(
    graph: nx.Graph,
    context: AdaptiveContext,
    partition: dict[str, int],
    cluster_demand: dict[int, float],
    node: str,
    source: int,
    target: int,
) -> float:
    cut_delta = 0.0
    semantic_cut_delta = 0.0
    for neighbor, attrs in graph[node].items():
        neighbor_cluster = partition[neighbor]
        before_cut = neighbor_cluster != source
        after_cut = neighbor_cluster != target
        if before_cut == after_cut:
            continue
        sign = 1.0 if after_cut else -1.0
        cut_delta += sign * max(float(attrs.get("weight", 0.0) or 0.0), 0.0)
        semantic = max(float(attrs.get("poi_similarity", 0.0) or 0.0), 0.0) + max(float(attrs.get("order_similarity", 0.0) or 0.0), 0.0)
        semantic_cut_delta += sign * semantic

    demand = context.demand[node]
    target_demand = context.target_demand + EPS
    before_capacity = (
        ((cluster_demand[source] - context.target_demand) / target_demand) ** 2
        + ((cluster_demand[target] - context.target_demand) / target_demand) ** 2
    )
    after_capacity = (
        ((cluster_demand[source] - demand - context.target_demand) / target_demand) ** 2
        + ((cluster_demand[target] + demand - context.target_demand) / target_demand) ** 2
    )
    return (
        context.params.lambda_cut * cut_delta
        + context.params.lambda_semantic_cut * semantic_cut_delta
        + context.params.lambda_local_capacity * (after_capacity - before_capacity)
    )


def relabel_partition(partition: dict[str, int]) -> dict[str, int]:
    labels = {old: new for new, old in enumerate(sorted(set(partition.values())))}
    return {node: labels[cluster_id] for node, cluster_id in partition.items()}
