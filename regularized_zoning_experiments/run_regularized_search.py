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
    require_keys(config["objective"]["grid"], ["lambda_c", "lambda_r"], "objective.grid")
    require_keys(
        config["search"],
        ["max_passes", "min_delta", "move_policy", "enforce_connectivity", "allow_merge_split"],
        "search",
    )

    if config["search"]["move_policy"] != "best_improving":
        raise ValueError("Only search.move_policy='best_improving' is implemented for V1.")
    if bool(config["search"]["allow_merge_split"]):
        raise ValueError("V1 implements only boundary single-segment moves; set allow_merge_split=false.")

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

    for grid_key in ["lambda_c", "lambda_r"]:
        values = config["objective"]["grid"].get(grid_key, [])
        if not isinstance(values, list) or not values:
            raise ValueError(f"objective.grid.{grid_key} must be a non-empty list.")


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
    lower = params.capacity_min_ratio * target
    upper = params.capacity_max_ratio * target
    low_violation = max(0.0, lower - value) / max(target, EPS)
    high_violation = max(0.0, value - upper) / max(target, EPS)
    return low_violation * low_violation + high_violation * high_violation


def capacity_component(cluster_demand: dict[int, float], context: SearchContext) -> float:
    if not cluster_demand:
        return 0.0
    target = context.total_demand / max(len(cluster_demand), 1)
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
    target: int,
) -> tuple[float, dict[str, float]]:
    graph_cut_delta = 0.0
    continuity_cut_delta = 0.0
    connector_cut_delta = 0.0
    for neighbor, attrs in context.graph[node].items():
        neighbor_cluster = partition[neighbor]
        before_cut = neighbor_cluster != source
        after_cut = neighbor_cluster != target
        if before_cut == after_cut:
            continue
        sign = 1.0 if after_cut else -1.0
        graph_cut_delta += sign * edge_value(attrs, "weight")
        continuity_cut_delta += sign * edge_value(attrs, "continuity_weight")
        connector_cut_delta += sign * edge_value(attrs, "connector_weight")

    cluster_count = max(len(cluster_demand), 1)
    target_demand = context.total_demand / cluster_count
    node_demand = context.demand[node]
    before_capacity = (
        capacity_penalty_for_value(cluster_demand[source], target_demand, context.objective)
        + capacity_penalty_for_value(cluster_demand[target], target_demand, context.objective)
    ) / cluster_count
    after_capacity = (
        capacity_penalty_for_value(cluster_demand[source] - node_demand, target_demand, context.objective)
        + capacity_penalty_for_value(cluster_demand[target] + node_demand, target_demand, context.objective)
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


def run_search(
    context: SearchContext,
    initial_partition: dict[str, int],
    init_name: str,
    setting_id: str,
) -> tuple[dict[str, int], list[dict[str, Any]], dict[str, float]]:
    partition = dict(initial_partition)
    assert_partition_valid(context.graph, partition)
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
                    "accepted_delta": best_delta,
                    **best_delta_parts,
                    **components,
                }
            )
        if moved_this_pass == 0:
            break

    final_components = objective_components(context, partition, cluster_demand)
    return partition, trace, final_components


def setting_id(lambda_c: float, lambda_r: float) -> str:
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

    graph = load_graph(project_path(config["inputs"]["graph"]))
    graph_nodes = sorted(str(node) for node in graph.nodes)
    demand = load_demand(project_path(config["inputs"]["order_features"]), graph_nodes)
    base_segments = gpd.read_file(project_path(config["inputs"]["segment_nodes"]))
    base_segments["seg_id"] = base_segments["seg_id"].astype(str)

    search_params = SearchParams(
        max_passes=int(config["search"]["max_passes"]),
        min_delta=float(config["search"]["min_delta"]),
        move_policy=str(config["search"]["move_policy"]),
        enforce_connectivity=bool(config["search"]["enforce_connectivity"]),
        allow_merge_split=bool(config["search"]["allow_merge_split"]),
    )
    overwrite = bool(config["outputs"].get("overwrite", True))
    graph_variant = str(config["scope"]["graph_variant"])

    manifest_rows = []
    all_trace_rows = []
    grid = config["objective"]["grid"]
    settings = list(itertools.product(grid["lambda_c"], grid["lambda_r"]))
    baseline_clusters = config["inputs"]["baseline_clusters"]

    for initialization in config["initializations"]:
        initial_partition = load_partition(project_path(baseline_clusters[initialization]), set(graph_nodes))
        algorithm = regularized_algorithm_name(initialization)
        for lambda_c, lambda_r in settings:
            current_setting_id = setting_id(float(lambda_c), float(lambda_r))
            print(f"Running {algorithm}/{current_setting_id}...")
            objective_params = ObjectiveParams(
                capacity_min_ratio=float(config["objective"]["capacity_min_ratio"]),
                capacity_max_ratio=float(config["objective"]["capacity_max_ratio"]),
                lambda_c=float(lambda_c),
                lambda_g=float(config["objective"]["lambda_g"]),
                lambda_r=float(lambda_r),
                alpha_cont=float(config["objective"]["alpha_cont"]),
                alpha_conn=float(config["objective"]["alpha_conn"]),
            )
            context = build_context(graph, demand, objective_params, search_params)
            partition, trace, final_components = run_search(context, initial_partition, initialization, current_setting_id)

            output_stem = f"segment_clusters_{graph_variant}_{algorithm}_{current_setting_id}"
            gpkg_path = clusters_dir / f"{output_stem}.gpkg"
            csv_path = clusters_dir / f"{output_stem}.csv"
            save_partition(gpkg_path, csv_path, base_segments, partition, initialization, current_setting_id, overwrite)

            params = {
                "initialization": initialization,
                "setting_id": current_setting_id,
                "capacity_min_ratio": objective_params.capacity_min_ratio,
                "capacity_max_ratio": objective_params.capacity_max_ratio,
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
            }
            manifest_rows.append(
                {
                    "graph_variant": graph_variant,
                    "algorithm": algorithm,
                    "initialization": initialization,
                    "setting_id": current_setting_id,
                    "lambda_c": objective_params.lambda_c,
                    "lambda_g": objective_params.lambda_g,
                    "lambda_r": objective_params.lambda_r,
                    "alpha_cont": objective_params.alpha_cont,
                    "alpha_conn": objective_params.alpha_conn,
                    "num_clusters": len(set(partition.values())),
                    "num_moves": max(len(trace) - 1, 0),
                    "clusters_gpkg": str(gpkg_path.relative_to(PROJECT_ROOT)),
                    "clusters_csv": str(csv_path.relative_to(PROJECT_ROOT)),
                    "params": json.dumps(params, sort_keys=True),
                    **final_components,
                }
            )
            all_trace_rows.extend(trace)

    manifest = pd.DataFrame(manifest_rows).sort_values(["initialization", "lambda_c", "lambda_r"]).reset_index(drop=True)
    manifest_path = tables_dir / "run_manifest.csv"
    manifest.to_csv(manifest_path, index=False)
    trace_frame = pd.DataFrame(all_trace_rows)
    trace_path = tables_dir / "objective_trace.csv"
    trace_frame.to_csv(trace_path, index=False)
    print(f"Saved manifest to {manifest_path}")
    print(f"Saved objective trace to {trace_path}")


if __name__ == "__main__":
    main()
