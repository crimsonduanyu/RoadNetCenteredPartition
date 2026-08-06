from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import networkx as nx
import pandas as pd
import yaml

from roadnet_partition.config import ResolvedStageConfig, stable_value
from roadnet_partition.io.geospatial import PROJECT_ROOT, display_path, project_path
from roadnet_partition.io.safe_graph import read_safe_graph, reject_legacy_graph_path
from roadnet_partition.pipeline.results import RunContext, StageResult, StageStatus
from roadnet_partition.zoning.contracts import save_partition
from roadnet_partition.zoning.regularized.objective import EPS, ObjectiveParams, build_context
from roadnet_partition.zoning.regularized.search import (
    SearchParams, normalize_partition_to_target, relabel_partition, run_search,
)
from roadnet_partition.zoning.regularized.selection import (
    build_settings, regularized_algorithm_name, setting_id,
)


def load_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)

def require_keys(config: dict[str, Any], keys: list[str], prefix: str = "config") -> None:
    for key in keys:
        if key not in config:
            raise ValueError(f"Missing required {prefix}.{key}")

def validate_config(config: dict[str, Any]) -> None:
    require_keys(config, ["scope", "inputs", "outputs", "initializations", "objective", "search"])
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
    graph = read_safe_graph(path)
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

def run_from_config(config: dict[str, Any], config_path: Path) -> None:
    """Run the regularized search grid for an already-loaded, validated config."""
    graph_path = project_path(config["inputs"]["graph"])
    # Refuse a legacy graph before the stage creates anything. Discovering it
    # at load time would already have left an output tree behind for a run that
    # can never complete.
    reject_legacy_graph_path(graph_path)
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

    graph = load_graph(graph_path)
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
                "clusters_gpkg": display_path(gpkg_path),
                "clusters_csv": display_path(csv_path),
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


def run_partition(
    config: ResolvedStageConfig,
    context: RunContext,
) -> StageResult:
    """Run regularized partitioning inside its owned stage directory."""
    if context.stage_dir is None or context.stage_name != "partition":
        raise ValueError("run_partition requires context.for_stage('partition')")
    resolved_output = context.stage_dir
    run_config = dict(stable_value(config.values))

    run_config["outputs"] = {**dict(run_config["outputs"]), "root": str(resolved_output)}
    validate_config(run_config)
    run_from_config(run_config, config.source_path)
    manifest_path = resolved_output / "tables" / "run_manifest.csv"
    outputs = {
        "resolved_config": resolved_output / "resolved_config.yaml",
        "manifest": manifest_path,
        "objective_trace": resolved_output / "tables" / "objective_trace.csv",
    }
    if not manifest_path.is_file():
        raise RuntimeError("Partition completed without run_manifest.csv")
    manifest = pd.read_csv(manifest_path)
    required_columns = {"algorithm", "setting_id", "clusters_gpkg", "clusters_csv"}
    missing_columns = required_columns - set(manifest.columns)
    if missing_columns:
        raise RuntimeError(f"Partition run manifest is missing columns: {sorted(missing_columns)}")
    for row in manifest.itertuples(index=False):
        label = f"{row.algorithm}_{row.setting_id}"
        for kind, value in (("gpkg", row.clusters_gpkg), ("csv", row.clusters_csv)):
            path = Path(value)
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            outputs[f"cluster_{kind}_{label}"] = path.resolve()
    missing = [path.name for path in outputs.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"Partition completed without required outputs: {missing}")
    return StageResult(
        stage="partition",
        status=StageStatus.COMPLETE,
        outputs=outputs,
    )
