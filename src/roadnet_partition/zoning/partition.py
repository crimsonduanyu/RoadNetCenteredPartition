from __future__ import annotations

import json
from pathlib import Path
import pickle
from typing import Any

import geopandas as gpd
import networkx as nx
import pandas as pd
import yaml

from roadnet_partition.config import ResolvedStageConfig, stable_value
from roadnet_partition.io.geospatial import (
    PROJECT_ROOT, display_path, ensure_scope_directories, get_scope_paths,
    load_config as load_unified_scope_config, project_path,
)
from roadnet_partition.pipeline.results import RunContext, StageResult, StageStatus
from roadnet_partition.zoning.algorithms.leiden import run_leiden
from roadnet_partition.zoning.algorithms.louvain import run_louvain
from roadnet_partition.zoning.algorithms.metis import run_metis
from roadnet_partition.zoning.algorithms.network_voronoi import run_demand_network_voronoi
from roadnet_partition.zoning.algorithms.region_growing import run_demand_region_growing
from roadnet_partition.zoning.algorithms.skater import run_skater
from roadnet_partition.zoning.contracts import save_baseline_partition_outputs, save_partition
from roadnet_partition.zoning.evaluate import build_ranked_summary, evaluate_partition
from roadnet_partition.zoning.regularized.objective import EPS, ObjectiveParams, build_context
from roadnet_partition.zoning.regularized.search import (
    SearchParams, normalize_partition_to_target, relabel_partition, run_search,
)
from roadnet_partition.zoning.regularized.selection import (
    build_settings, regularized_algorithm_name, setting_id,
)


ALGORITHM_RUNNERS = {
    "louvain": run_louvain,
    "leiden": run_leiden,
    "skater": run_skater,
    "metis": run_metis,
    "demand_network_voronoi": run_demand_network_voronoi,
    "demand_region_growing": run_demand_region_growing,
}

CONFIG_PATH = PROJECT_ROOT / "config.yaml"

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
    """Run the full regularized search grid for an already-loaded, validated config.

    Shared by the experiment CLI (``run_regularized_search.main``) and the unified
    Stage 1 runner (``src/stages/stage1_partition.py``); both build the config dict
    and delegate here, so the search numerics live in exactly one place.
    """
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
    config: ResolvedStageConfig | dict[str, Any],
    context_or_output_root: RunContext | Path,
    config_path: Path | None = None,
) -> StageResult:
    """Run regularized partitioning through the new stage API or legacy call."""
    if isinstance(config, ResolvedStageConfig):
        if config_path is not None or not isinstance(context_or_output_root, RunContext):
            raise TypeError("new run_partition call requires (ResolvedStageConfig, RunContext)")
        context = context_or_output_root
        if context.stage_dir is None or context.stage_name != "partition":
            raise ValueError("run_partition requires context.for_stage('partition')")
        resolved_output = context.stage_dir
        run_config = dict(stable_value(config.values))
        source_path = config.source_path
    else:
        if isinstance(context_or_output_root, RunContext) or config_path is None:
            raise TypeError("legacy run_partition call requires (dict, output_root, config_path)")
        resolved_output = Path(context_or_output_root).expanduser().resolve()
        run_config = dict(config)
        source_path = Path(config_path).expanduser().resolve()

    run_config["outputs"] = {**dict(run_config["outputs"]), "root": str(resolved_output)}
    validate_config(run_config)
    run_from_config(run_config, source_path)
    if isinstance(config, ResolvedStageConfig):
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
    else:
        outputs = {
            "root": resolved_output,
            "manifest": resolved_output / "tables" / "run_manifest.csv",
            "objective_trace": resolved_output / "tables" / "objective_trace.csv",
        }
    return StageResult(
        stage="partition",
        status=StageStatus.COMPLETE,
        outputs=outputs,
    )


def run_baseline_partition(config: dict[str, Any], output_root: Path) -> StageResult:
    """Run configured baseline algorithms beneath an explicit output root."""
    root = Path(output_root).expanduser().resolve()
    paths = get_scope_paths(config)
    paths["input_data_processed"] = paths["data_processed"]
    paths["data_processed"] = root / "partition"
    paths["outputs_tables"] = root / "metrics"
    paths["data_processed"].mkdir(parents=True, exist_ok=True)
    paths["outputs_tables"].mkdir(parents=True, exist_ok=True)
    _run_baseline_partition(config, paths)
    return StageResult(
        stage="partition",
        status=StageStatus.COMPLETE,
        outputs={"partition": paths["data_processed"], "metrics": paths["outputs_tables"]},
    )


def _run_baseline_partition(config: dict[str, Any], paths: dict[str, Path]) -> None:
    algorithms = config["clustering"].get("algorithms", [config["clustering"].get("method", "louvain")])
    unknown = [algorithm for algorithm in algorithms if algorithm not in ALGORITHM_RUNNERS]
    if unknown:
        raise ValueError(f"Unknown clustering algorithms: {unknown}. Expected one of {list(ALGORITHM_RUNNERS)}.")

    nodes_path = paths["segment_nodes"]
    print(f"Loading segment nodes from {nodes_path}...")
    base_segments = gpd.read_file(nodes_path)
    evaluation_rows = []

    for graph_variant in config["semantic_graph"]["variants"]:
        graph_path = paths["outputs_graphs"] / f"segment_relation_graph_{graph_variant}.gpickle"
        edge_path = paths.get("input_data_processed", paths["data_processed"]) / f"segment_relation_edges_{graph_variant}.csv"
        print(f"Loading {graph_variant} graph from {graph_path}...")
        with graph_path.open("rb") as handle:
            graph = pickle.load(handle)
        edges = pd.read_csv(edge_path)
        for algorithm in algorithms:
            print(f"Running {algorithm} on {graph_variant}...")
            current_partition = ALGORITHM_RUNNERS[algorithm](graph, config)
            segments, summary, diagnostics = save_baseline_partition_outputs(
                graph_variant, algorithm, base_segments, current_partition, config, paths,
            )
            evaluation_rows.append(evaluate_partition(
                graph_variant, algorithm, graph, edges, segments, current_partition,
                summary, diagnostics, paths,
            ))

    evaluation = pd.DataFrame(evaluation_rows)
    evaluation_path = paths["outputs_tables"] / "graph_algorithm_evaluation.csv"
    evaluation.to_csv(evaluation_path, index=False)
    comparison_path = paths["outputs_tables"] / "comparison_evaluation.csv"
    evaluation.to_csv(comparison_path, index=False)
    ranked_path = paths["outputs_tables"] / "graph_algorithm_ranked_summary.csv"
    build_ranked_summary(evaluation).to_csv(ranked_path, index=False)
    louvain_only = evaluation.loc[evaluation["algorithm"] == "louvain"].rename(columns={"graph_variant": "variant"})
    louvain_only.drop(columns=["algorithm"]).to_csv(paths["outputs_tables"] / "graph_variant_evaluation.csv", index=False)
    print(f"Saved graph algorithm evaluation to {evaluation_path}")
    print(f"Saved comparison evaluation to {comparison_path}")
    print(f"Saved ranked summary to {ranked_path}")


def legacy_baseline_main() -> None:
    config = load_unified_scope_config()
    ensure_scope_directories(config)
    _run_baseline_partition(config, get_scope_paths(config))


def load_unified_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def build_regularized_config(unified: dict, output_root: str) -> dict:
    stage1 = unified["stage1_partition"]
    reg = stage1["regularized"]
    search_config = dict(reg["search"])
    search_config["allow_merge_split"] = False
    search_config["grid"] = {**dict(search_config.get("grid", {})), "merge_split_enabled": [False]}
    return {
        "scope": {"active": unified["study_area"]["active"], "graph_variant": stage1["graph_variant"]},
        "inputs": {**reg["inputs"], "baseline_clusters": reg["baseline_clusters"]},
        "outputs": {"root": output_root, "overwrite": True, "resume": False},
        "initializations": [reg["initialization"]],
        "objective": reg["objective"],
        "search": search_config,
        "evaluation": reg["evaluation"],
    }


def verify_output_root(unified: dict) -> Path:
    run_root = Path(unified["stage1_partition"]["outputs"]["run_root"])
    return run_root.parent / f"{run_root.name}_stage1_verify"


def canonical_setting_stem(unified: dict) -> str:
    stage1 = unified["stage1_partition"]
    reg = stage1["regularized"]
    obj = reg["objective"]
    algorithm = regularized_algorithm_name(reg["initialization"])
    from roadnet_partition.zoning.regularized.selection import SearchSetting
    setting = SearchSetting(
        lambda_c=float(obj["grid"]["lambda_c"][0]),
        lambda_r=float(obj["grid"].get("lambda_r", [obj["lambda_r"]])[0]),
        alpha_cont=float(obj["grid"].get("alpha_cont", [obj["alpha_cont"]])[0]),
        alpha_conn=float(obj["grid"].get("alpha_conn", [obj["alpha_conn"]])[0]),
        merge_split_enabled=False,
    )
    return f"segment_clusters_{stage1['graph_variant']}_{algorithm}_{setting_id(setting)}"


def verify_against_frozen(unified: dict, regenerated_stem: str) -> bool:
    from roadnet_partition.zoning.contracts import partition_groups
    frozen = project_path(unified["stage1_partition"]["outputs"]["canonical_partition"])
    regenerated = verify_output_root(unified) / "clusters" / f"{regenerated_stem}.gpkg"
    if not regenerated.exists():
        raise FileNotFoundError(f"Regenerated partition not found: {regenerated}")
    frozen_groups = partition_groups(gpd.read_file(frozen))
    regen_groups = partition_groups(gpd.read_file(regenerated))
    equivalent = frozen_groups == regen_groups
    print(f"frozen canonical : {frozen}  ({len(frozen_groups)} clusters)")
    print(f"regenerated      : {regenerated}  ({len(regen_groups)} clusters)")
    print(f"REPRODUCTION {'PASS: partitions are equivalent' if equivalent else 'FAIL: partitions differ'}")
    return equivalent


def legacy_stage1_main(argv: list[str] | None = None) -> None:
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    verify = "--verify" in argv
    verify_only = "--verify-only" in argv
    unified = load_unified_config()
    stem = canonical_setting_stem(unified)
    output_root = verify_output_root(unified)
    if not verify_only:
        config = build_regularized_config(unified, str(output_root))
        validate_config(config)
        print(f"Running regularized search -> {output_root}")
        run_from_config(config, CONFIG_PATH)
    if verify or verify_only:
        ok = verify_against_frozen(unified, stem)
        sys.exit(0 if ok else 1)
