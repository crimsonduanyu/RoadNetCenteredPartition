from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from roadnet_partition.config import ConfigError, ResolvedStageConfig, apply_stage_overrides, stable_value
from roadnet_partition.io.manifests import (
    RUN_MARKER,
    SUCCESS_MARKER,
    assert_run_fingerprints,
    atomic_write_json,
    begin_stage,
    complete_stage,
    end_stage_with_status,
    evaluate_resume,
    file_record,
    initialize_run,
    input_fingerprint,
    invalidate_from_stage,
    load_manifest,
    verify_run_ownership,
)
from roadnet_partition.io.paths import assert_safe_run_dir
from roadnet_partition.pipeline.results import RunContext, StageResult, StageStatus


RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RESOLVED_CONFIG_FILENAME = "resolved_config.json"
_SOURCE_PROJECT_ROOT = Path(__file__).resolve().parents[3]
STAGE_ORDER = ("partition", "demand", "supply", "tte")
PIPELINE_BINDINGS = {
    "demand": (
        ("partition", "canonical_partition", "order_pipeline.inputs.partition_gpkg", "partition"),
    ),
    "supply": (
        ("demand", "orders_region_assigned", "stage3_supply.orders_path", "assigned_orders"),
        ("demand", "cluster_index", "stage3_supply.cluster_index_path", "cluster_index"),
    ),
    "tte": (
        ("demand", "orders_region_assigned", "stage4_tte.inputs.orders_path", "assigned_orders"),
        ("demand", "cluster_index", "stage4_tte.inputs.cluster_index_path", "cluster_index"),
    ),
}


class RunConflictError(RuntimeError):
    pass


class ResumeConflictError(RunConflictError):
    pass


class StageContractError(RuntimeError):
    pass


def default_run_id(config: ResolvedStageConfig) -> str:
    if not config.stage or not config.scope:
        raise ConfigError(f"{config.source_path}: resolved stage/scope metadata is missing")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{config.scope}-{config.stage}-{config.fingerprint[:8]}"


def _validate_run_id(run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
        raise ConfigError(f"invalid run ID: {run_id!r}")
    return run_id


def _required_file(path: str | Path, *, config: ResolvedStageConfig, field: str) -> Path:
    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"{config.source_path}: {field} does not exist or is not a file: {source}")
    return source


def _partition_inputs(config: ResolvedStageConfig) -> dict[str, dict[str, Any]]:
    values = config.values
    inputs = dict(values["inputs"])
    records = {}
    for key in (
        "graph", "relation_edges", "classified_edges", "segment_nodes",
        "poi_features", "order_features",
    ):
        if key in inputs:
            records[key] = file_record(_required_file(inputs[key], config=config, field=f"inputs.{key}"))
    baselines = dict(inputs["baseline_clusters"])
    for name in values["initializations"]:
        if name not in baselines:
            raise ConfigError(f"{config.source_path}: inputs.baseline_clusters.{name} is missing")
        value = baselines[name]
        records[f"baseline_clusters.{name}"] = file_record(
            _required_file(value, config=config, field=f"inputs.baseline_clusters.{name}")
        )
    contract = dict(values.get("contract", {}))
    if bool(contract.get("verify_canonical", False)):
        expected = contract.get("expected_partition")
        if not expected:
            raise ConfigError(f"{config.source_path}: contract.expected_partition is required when verification is enabled")
        records["canonical_expected"] = file_record(
            _required_file(expected, config=config, field="contract.expected_partition")
        )
    return records


def _demand_inputs(config: ResolvedStageConfig) -> dict[str, dict[str, Any]]:
    inputs = dict(config.values["order_pipeline"]["inputs"])
    records = {
        "partition": file_record(_required_file(inputs["partition_gpkg"], config=config, field="order_pipeline.inputs.partition_gpkg")),
        "road_relation_edges": file_record(_required_file(
            inputs["road_relation_edges_csv"], config=config,
            field="order_pipeline.inputs.road_relation_edges_csv",
        )),
        "poi": file_record(_required_file(inputs["poi_path"], config=config, field="order_pipeline.inputs.poi_path")),
    }
    for index, value in enumerate(inputs["order_datasets"]):
        records[f"orders.{index}"] = file_record(
            _required_file(value, config=config, field=f"order_pipeline.inputs.order_datasets[{index}]")
        )
    return records


def _cluster_universe_record(orders_path: Path) -> dict[str, Any]:
    import pandas as pd

    clusters: set[str] = set()
    for chunk in pd.read_csv(
        orders_path,
        usecols=["origin_cluster_id", "destination_cluster_id"],
        dtype={"origin_cluster_id": str, "destination_cluster_id": str},
        chunksize=500_000,
    ):
        clusters.update(chunk["origin_cluster_id"].dropna())
        clusters.update(chunk["destination_cluster_id"].dropna())
    ordered = sorted(clusters, key=lambda value: (0, int(value)) if value.lstrip("-").isdigit() else (1, value))
    digest = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()
    return {"source": str(orders_path.resolve()), "count": len(ordered), "sha256": digest}


def _supply_inputs(config: ResolvedStageConfig) -> dict[str, dict[str, Any]]:
    stage = dict(config.values["stage3_supply"])
    orders = _required_file(stage["orders_path"], config=config, field="stage3_supply.orders_path")
    records = {"assigned_orders": file_record(orders)}
    cluster_index = stage.get("cluster_index_path")
    if cluster_index:
        records["cluster_index"] = file_record(
            _required_file(cluster_index, config=config, field="stage3_supply.cluster_index_path")
        )
    else:
        records["cluster_universe"] = _cluster_universe_record(orders)
    return records


def _tte_inputs(config: ResolvedStageConfig) -> dict[str, dict[str, Any]]:
    stage = dict(config.values["stage4_tte"])
    inputs = dict(stage["inputs"])
    records = {
        "assigned_orders": file_record(_required_file(
            inputs["orders_path"], config=config, field="stage4_tte.inputs.orders_path",
        )),
        "cluster_index": file_record(_required_file(
            inputs["cluster_index_path"], config=config, field="stage4_tte.inputs.cluster_index_path",
        )),
    }
    if inputs.get("network_distance_path"):
        records["network_distance"] = file_record(_required_file(
            inputs["network_distance_path"], config=config,
            field="stage4_tte.inputs.network_distance_path",
        ))
        records["representative_nodes"] = file_record(_required_file(
            inputs["representative_nodes_path"], config=config,
            field="stage4_tte.inputs.representative_nodes_path",
        ))
    else:
        distance = dict(stage["distance"])
        for key in ("graphml_path", "classified_edges_path", "partition_gpkg"):
            records[f"distance.{key}"] = file_record(
                _required_file(distance[key], config=config, field=f"stage4_tte.distance.{key}")
            )
    return records


_INPUT_COLLECTORS: dict[str, Callable[[ResolvedStageConfig], dict[str, dict[str, Any]]]] = {
    "partition": _partition_inputs,
    "demand": _demand_inputs,
    "supply": _supply_inputs,
    "tte": _tte_inputs,
}


def collect_stage_inputs(stage: str, config: ResolvedStageConfig) -> dict[str, dict[str, Any]]:
    try:
        collector = _INPUT_COLLECTORS[stage]
    except KeyError as error:
        raise ConfigError(f"unknown stage: {stage!r}") from error
    if config.stage != stage:
        raise ConfigError(f"{config.source_path}: resolved stage {config.stage!r} does not match {stage!r}")
    return collector(config)


def _partition_outputs(config: ResolvedStageConfig, stage_dir: Path) -> dict[str, Path]:
    from roadnet_partition.zoning.regularized.selection import (
        build_settings,
        regularized_algorithm_name,
        setting_id,
    )

    values = dict(config.values)
    outputs = {
        "resolved_config": stage_dir / "resolved_config.yaml",
        "manifest": stage_dir / "tables" / "run_manifest.csv",
        "objective_trace": stage_dir / "tables" / "objective_trace.csv",
    }
    variant = str(values["scope"]["graph_variant"])
    for initialization in values["initializations"]:
        algorithm = regularized_algorithm_name(str(initialization))
        for setting in build_settings(values):
            label = f"{algorithm}_{setting_id(setting)}"
            stem = f"segment_clusters_{variant}_{label}"
            outputs[f"cluster_gpkg_{label}"] = stage_dir / "clusters" / f"{stem}.gpkg"
            outputs[f"cluster_csv_{label}"] = stage_dir / "clusters" / f"{stem}.csv"
    return outputs


def _demand_outputs(config: ResolvedStageConfig, stage_dir: Path) -> dict[str, Path]:
    suffix = f"{int(config.values['order_pipeline']['time_slot_minutes'])}min"
    names = {
        "cluster_index": "cluster_index.csv",
        "orders_region_assigned": "orders_region_assigned.csv.gz",
        "cluster_od": f"cluster_od_{suffix}.csv",
        "od_tensor": f"od_tensor_{suffix}.npz",
        "metadata": "metadata.json",
        "road_graph_edges": "cluster_graph_road_edges.csv",
        "road_adjacency_raw": "cluster_graph_road_adjacency_raw.npz",
        "road_adjacency_normalized": "cluster_graph_road_adjacency_normalized.npz",
        "poi_graph_edges": "cluster_graph_poi_edges.csv",
        "poi_adjacency_raw": "cluster_graph_poi_adjacency_raw.npz",
        "poi_adjacency_normalized": "cluster_graph_poi_adjacency_normalized.npz",
        "distance_graph_edges": "cluster_graph_distance_edges.csv",
        "distance_adjacency_raw": "cluster_graph_distance_adjacency_raw.npz",
        "distance_adjacency_normalized": "cluster_graph_distance_adjacency_normalized.npz",
        "poi_features": "cluster_poi_features.csv",
        "poi_category_mapping": "cluster_poi_category_mapping.csv",
    }
    return {name: stage_dir / filename for name, filename in names.items()}


def _supply_outputs(_config: ResolvedStageConfig, stage_dir: Path) -> dict[str, Path]:
    return {
        "inservice_od": stage_dir / "supply_inservice_od.csv.gz",
        "available_floor": stage_dir / "supply_available_floor.csv.gz",
        "fleet_lower_bound": stage_dir / "supply_fleet_lower_bound.csv.gz",
        "run_summary": stage_dir / "run_summary.json",
        "config_used": stage_dir / "config_used.json",
    }


def _tte_outputs(config: ResolvedStageConfig, stage_dir: Path) -> dict[str, Path]:
    stage = dict(config.values["stage4_tte"])
    outputs = dict(stage.get("outputs", {}))
    distance = dict(stage.get("distance", {}))
    return {
        "network_distance": stage_dir / str(distance.get("matrix_filename", "cluster_network_distance.parquet")),
        "representative_nodes": stage_dir / str(distance.get("representatives_filename", "cluster_representative_nodes.csv")),
        "tte_raw": stage_dir / "TTE_raw.parquet",
        "tte_count": stage_dir / str(outputs.get("count_filename", "TTE_count.parquet")),
        "tte_support": stage_dir / str(outputs.get("support_filename", "TTE_support.parquet")),
        "tte_hops": stage_dir / str(outputs.get("hops_filename", "TTE_hops.parquet")),
        "tte_imputed": stage_dir / "TTE_imputed.parquet",
    }


_OUTPUT_COLLECTORS: dict[str, Callable[[ResolvedStageConfig, Path], dict[str, Path]]] = {
    "partition": _partition_outputs,
    "demand": _demand_outputs,
    "supply": _supply_outputs,
    "tte": _tte_outputs,
}


def formal_stage_outputs(stage: str, config: ResolvedStageConfig, stage_dir: Path) -> dict[str, Path]:
    return _OUTPUT_COLLECTORS[stage](config, stage_dir)


def prepare_stage_contract_config(
    stage: str,
    config: ResolvedStageConfig,
    input_records: Mapping[str, Mapping[str, Any]],
) -> ResolvedStageConfig:
    if stage != "partition":
        return config
    original = Path(config.values["inputs"]["segment_nodes"])
    if original.is_file():
        return config
    candidate = config.project_root / "data/interim" / config.scope / "frozen_inputs" / original.name
    expected = input_records["segment_nodes"]
    actual = file_record(candidate)
    if actual["size"] != expected["size"] or actual["sha256"] != expected["sha256"]:
        raise ValueError("relocated Partition segment_nodes differs from source run input")
    values = deepcopy(config.values)
    values["inputs"]["segment_nodes"] = candidate.as_posix()
    return replace(config, values=values)


def canonical_partition_output_key(outputs: Mapping[str, Any]) -> str:
    keys = sorted(name for name in outputs if name.startswith("cluster_gpkg_"))
    if len(keys) != 1:
        raise StageContractError(f"pipeline Partition requires exactly one cluster_gpkg_* output, found {keys}")
    return keys[0]


def _partition_contract(config: ResolvedStageConfig, outputs: Mapping[str, Path]) -> dict[str, Any]:
    import geopandas as gpd
    import pandas as pd

    from roadnet_partition.zoning.contracts import compare_partitions, validate_partition

    segment_nodes = gpd.read_file(config.values["inputs"]["segment_nodes"])
    expected_ids = segment_nodes["seg_id"].astype(str)
    summaries = []
    gpkg_outputs = [path for name, path in outputs.items() if name.startswith("cluster_gpkg_")]
    for gpkg_path in gpkg_outputs:
        clusters = gpd.read_file(gpkg_path)
        summary = validate_partition(
            clusters,
            expected_segment_ids=expected_ids,
            expected_crs=segment_nodes.crs,
            expected_bounds=segment_nodes.total_bounds,
        )
        csv_path = gpkg_path.with_suffix(".csv")
        csv = pd.read_csv(csv_path, dtype={"seg_id": str})
        gpkg_mapping = clusters[["seg_id", "cluster_id"]].copy()
        gpkg_mapping["seg_id"] = gpkg_mapping["seg_id"].astype(str)
        csv_mapping = csv[["seg_id", "cluster_id"]].copy()
        pd.testing.assert_frame_equal(
            gpkg_mapping.sort_values("seg_id").reset_index(drop=True),
            csv_mapping.sort_values("seg_id").reset_index(drop=True),
            check_dtype=False,
        )
        summaries.append(summary)
    contract = dict(config.values.get("contract", {}))
    verified = False
    if bool(contract.get("verify_canonical", False)):
        if len(gpkg_outputs) != 1:
            raise ValueError("canonical Partition verification requires exactly one generated candidate")
        expected = gpd.read_file(contract["expected_partition"])
        actual = gpd.read_file(gpkg_outputs[0])
        if not compare_partitions(actual, expected):
            raise ValueError("generated Partition differs from the configured canonical partition")
        verified = True
    return {
        "status": "passed",
        "candidates": len(summaries),
        "segment_count": 0 if not summaries else summaries[0]["segment_count"],
        "cluster_counts": [summary["cluster_count"] for summary in summaries],
        "canonical_verified": verified,
    }


def _demand_contract(config: ResolvedStageConfig, outputs: Mapping[str, Path]) -> dict[str, Any]:
    import geopandas as gpd
    import pandas as pd

    from roadnet_partition.downstream.demand_contracts import (
        validate_assigned_orders,
        validate_cluster_index,
        validate_graph_assets,
        validate_metadata,
        validate_od_and_tensor,
    )

    pipeline = dict(config.values["order_pipeline"])
    partition = gpd.read_file(pipeline["inputs"]["partition_gpkg"])
    cluster_index = pd.read_csv(outputs["cluster_index"], dtype={"cluster_id": str})
    cluster_ids = validate_cluster_index(cluster_index, partition["cluster_id"].astype(str))
    assigned = validate_assigned_orders(
        outputs["orders_region_assigned"],
        cluster_ids,
        chunksize=int(config.values.get("contract", {}).get("assigned_chunksize", 500_000)),
    )
    od = validate_od_and_tensor(outputs["cluster_od"], outputs["od_tensor"], cluster_ids)
    normalization = dict(pipeline.get("graph_normalization", {}))
    graphs = {
        name: validate_graph_assets(
            outputs["cluster_index"].parent,
            name,
            len(cluster_ids),
            add_self_loops=bool(normalization.get("add_self_loops", True)),
            symmetric=bool(normalization.get("symmetric", True)),
        )
        for name in ("road", "poi", "distance")
    }
    metadata = validate_metadata(
        outputs["metadata"], cluster_count=len(cluster_ids), tensor_slots=int(od["shape"][0]),
    )
    return {
        "status": "passed",
        "clusters": len(cluster_ids),
        "assigned_rows": assigned["rows"],
        "od_rows": od["rows"],
        "tensor_shape": list(od["shape"]),
        "graph_rows": {name: summary["rows"] for name, summary in graphs.items()},
        "metadata_scope": metadata["active_scope"],
    }


def _supply_contract(config: ResolvedStageConfig, outputs: Mapping[str, Path]) -> dict[str, Any]:
    import pandas as pd

    from roadnet_partition.downstream.supply_contracts import validate_supply_outputs

    stage = dict(config.values["stage3_supply"])
    expected_clusters = None
    if stage.get("cluster_index_path"):
        expected_clusters = pd.read_csv(stage["cluster_index_path"], dtype={"cluster_id": str})["cluster_id"]
    validated = validate_supply_outputs(
        outputs["run_summary"].parent,
        expected_cluster_ids=expected_clusters,
        expected_config={
            "max_gap_minutes": int(stage["max_gap_minutes"]),
            "tau_idle_minutes": int(stage["tau_idle_minutes"]),
            "carpool_merge_gap_s": int(stage["carpool_merge_gap_s"]),
            "slot_duration_min": int(stage["slot_duration_min"]),
            "n_blocks": int(stage["n_blocks"]),
        },
        chunksize=int(config.values.get("contract", {}).get("chunksize", 500_000)),
    )
    return {
        "status": "passed",
        "clusters": int(validated["run_summary"]["global_clusters"]),
        "slots": int(validated["run_summary"]["global_slots"]),
        "orders": int(validated["run_summary"]["orders_loaded"]),
    }


def _tte_contract(config: ResolvedStageConfig, outputs: Mapping[str, Path]) -> dict[str, Any]:
    import pandas as pd

    from roadnet_partition.downstream.tte_contracts import validate_tte_outputs

    stage = dict(config.values["stage4_tte"])
    time = dict(stage["time"])
    validated = validate_tte_outputs(
        outputs["tte_raw"].parent,
        count_filename=outputs["tte_count"].name,
        support_filename=outputs["tte_support"].name,
        hops_filename=outputs["tte_hops"].name,
        matrix_filename=outputs["network_distance"].name,
        representatives_filename=outputs["representative_nodes"].name,
        expected_time_index=pd.date_range(start=time["start_time"], end=time["end_time"], freq=str(time["freq"])),
        raw_range=(float(stage["trip_time"]["min_minutes"]), float(stage["trip_time"]["max_minutes"])),
        max_hops=int(stage.get("imputation", {}).get("max_hops", 3)),
        batch_size=int(config.values.get("contract", {}).get("batch_size", 64)),
    )
    return {
        "status": "passed",
        "shape": validated["shape"],
        "observed_cells": int(validated["observed_cells"]),
        "inferred_cells": int(validated["inferred_cells"]),
        "missing_cells": int(validated["missing_cells"]),
    }


_CONTRACTS: dict[str, Callable[[ResolvedStageConfig, Mapping[str, Path]], dict[str, Any]]] = {
    "partition": _partition_contract,
    "demand": _demand_contract,
    "supply": _supply_contract,
    "tte": _tte_contract,
}


def validate_stage_contract(
    stage: str,
    config: ResolvedStageConfig,
    outputs: Mapping[str, Path],
) -> dict[str, Any]:
    try:
        return _CONTRACTS[stage](config, outputs)
    except StageContractError:
        raise
    except Exception as error:
        raise StageContractError(f"{stage} contract failed: {error}") from error


def _run_stage(stage: str, config: ResolvedStageConfig, context: RunContext) -> StageResult:
    if stage == "partition":
        from roadnet_partition.zoning.partition import run_partition
        return run_partition(config, context)
    if stage == "demand":
        from roadnet_partition.downstream.demand import run_demand
        return run_demand(config, context)
    if stage == "supply":
        from roadnet_partition.downstream.supply import run_supply
        return run_supply(config, context)
    if stage == "tte":
        from roadnet_partition.downstream.tte import run_tte
        return run_tte(config, context)
    raise ConfigError(f"unknown stage: {stage!r}")


def _resolved_document(config: ResolvedStageConfig) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_path": str(config.source_path),
        "dataset_path": None if config.dataset_path is None else str(config.dataset_path),
        "fingerprint": config.fingerprint,
        "resolved": stable_value(config.values),
    }


def _load_existing_context(
    run_dir: Path,
    *,
    project_root: Path,
    requested_run_id: str | None,
) -> RunContext:
    marker_path = run_dir / RUN_MARKER
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        existing_run_id = str(marker["run_id"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RunConflictError(f"existing run directory has no valid ownership marker: {run_dir}") from error
    if requested_run_id is not None and requested_run_id != existing_run_id:
        raise RunConflictError(
            f"requested run ID {requested_run_id!r} differs from owned run ID {existing_run_id!r}"
        )
    return RunContext(existing_run_id, run_dir, project_root, log_dir=run_dir / "logs")


def _resume_result(stage: str, context: RunContext, outputs: Mapping[str, Path]) -> StageResult:
    manifest = load_manifest(context.run_dir)
    record = manifest["stages"][stage]
    return StageResult(
        stage=stage,
        status=StageStatus.COMPLETE,
        outputs=outputs,
        metrics={**dict(record.get("metrics", {})), "resume_reused": True},
        contract=dict(record.get("contract", {})),
    )


def _remove_success_marker(stage_dir: Path, error: BaseException) -> None:
    try:
        (stage_dir / SUCCESS_MARKER).unlink(missing_ok=True)
    except OSError as cleanup_error:
        error.add_note(f"could not remove incomplete success marker: {cleanup_error}")


def execute_stage(
    *,
    stage: str,
    config: ResolvedStageConfig,
    run_dir: Path | None = None,
    run_id: str | None = None,
    resume: bool = False,
    overwrite: bool = False,
    overrides: Mapping[str, Any] | None = None,
    prepared_run_context: RunContext | None = None,
    prepared_inputs: Mapping[str, Mapping[str, Any]] | None = None,
    runtime_bindings: list[Mapping[str, Any]] | None = None,
) -> StageResult:
    if resume and overwrite:
        raise RunConflictError("--resume and --overwrite are mutually exclusive")
    config = apply_stage_overrides(config, overrides)
    if config.project_root is None or config.scope is None:
        raise ConfigError(f"{config.source_path}: resolver did not provide project_root/scope")
    project_root = config.project_root.resolve()
    if prepared_run_context is not None and (run_dir is not None or run_id is not None or overwrite):
        raise RunConflictError("prepared pipeline execution owns run_dir/run_id/overwrite")
    requested_run_id = None if run_id is None else _validate_run_id(run_id)
    generated_run_id = _validate_run_id(requested_run_id or default_run_id(config))
    destination = (
        prepared_run_context.run_dir
        if prepared_run_context is not None
        else Path(run_dir) if run_dir is not None
        else project_root / "outputs" / "runs" / generated_run_id
    )
    resolved_metadata = dict(config.values.get("_resolved", {}))
    destination = assert_safe_run_dir(
        destination,
        project_root,
        additional_project_roots=(_SOURCE_PROJECT_ROOT,),
        protected_roots=tuple(Path(value) for value in resolved_metadata.get("dataset_roots", [])),
    )
    existed = destination.exists()
    nonempty = existed and any(destination.iterdir())
    inputs = dict(prepared_inputs) if prepared_inputs is not None else collect_stage_inputs(stage, config)
    inputs_fingerprint = input_fingerprint(inputs)

    if prepared_run_context is not None:
        context = prepared_run_context
        if context.run_dir.resolve() != destination.resolve() or context.project_root.resolve() != project_root:
            raise RunConflictError("prepared run context differs from resolved pipeline context")
        verify_run_ownership(context)
    elif nonempty:
        if not (resume or overwrite):
            raise RunConflictError(f"run directory already exists; use --resume or --overwrite: {destination}")
        context = _load_existing_context(
            destination, project_root=project_root, requested_run_id=requested_run_id,
        )
        manifest = verify_run_ownership(context)
        unexpected_stages = set(manifest["stages"]) - {stage}
        if unexpected_stages:
            raise RunConflictError(f"single-stage run contains other stages: {sorted(unexpected_stages)}")
        try:
            assert_run_fingerprints(
                manifest,
                config_fingerprint=config.fingerprint,
                inputs_fingerprint=inputs_fingerprint,
            )
        except ValueError as error:
            conflict = ResumeConflictError if resume else RunConflictError
            raise conflict(str(error)) from error
        resolved_path = destination / RESOLVED_CONFIG_FILENAME
        if resume:
            try:
                if json.loads(resolved_path.read_text(encoding="utf-8")) != _resolved_document(config):
                    raise ResumeConflictError("resolved config document differs")
            except (OSError, json.JSONDecodeError) as error:
                raise ResumeConflictError("resolved config document missing or invalid") from error
        else:
            atomic_write_json(resolved_path, _resolved_document(config))
    else:
        if existed and not (resume or overwrite):
            raise RunConflictError(f"existing run directory requires --resume or --overwrite: {destination}")
        if resume:
            raise ResumeConflictError(f"cannot resume a missing or empty run directory: {destination}")
        context = initialize_run(
            destination,
            run_id=generated_run_id,
            scope=config.scope,
            project_root=project_root,
            config_source=config.source_path,
            config_values=config.values,
            config_fingerprint=config.fingerprint,
            inputs=inputs,
        )
        atomic_write_json(destination / RESOLVED_CONFIG_FILENAME, _resolved_document(config))

    stage_context = context.for_stage(stage)
    required_outputs = formal_stage_outputs(stage, config, stage_context.stage_dir)
    manifest = verify_run_ownership(context)
    record = manifest["stages"].get(stage)
    if resume and record is None and stage_context.stage_dir.exists() and any(stage_context.stage_dir.iterdir()):
        raise RunConflictError(f"unregistered stage directory is not reusable: {stage_context.stage_dir}")
    if resume and record and record.get("status") == StageStatus.COMPLETE.value:
        decision = evaluate_resume(
            stage_context,
            config_fingerprint=config.fingerprint,
            inputs_fingerprint=inputs_fingerprint,
            required_outputs=required_outputs,
            require_run_complete=prepared_run_context is None,
        )
        if not decision.reusable:
            raise ResumeConflictError("resume rejected: " + "; ".join(decision.reasons))
        return _resume_result(stage, context, required_outputs)

    if overwrite and nonempty:
        if record is None and stage_context.stage_dir.exists():
            raise RunConflictError("stage directory is not owned by the manifest")
        invalidate_from_stage(context, ordered_stages=[stage], from_stage=stage)
    elif resume and record:
        status = record.get("status")
        if status == StageStatus.RUNNING.value:
            raise ResumeConflictError("resume rejected: stage is still marked running")
        if prepared_run_context is not None and status == StageStatus.NOT_STARTED.value:
            pass
        elif status in {StageStatus.FAILED.value, StageStatus.INTERRUPTED.value, StageStatus.NOT_STARTED.value}:
            invalidate_from_stage(context, ordered_stages=[stage], from_stage=stage)
        else:
            raise ResumeConflictError(f"resume rejected: unsupported stage status {status!r}")

    try:
        begin_stage(
            stage_context,
            config_fingerprint=config.fingerprint,
            inputs_fingerprint=inputs_fingerprint,
            inputs=inputs,
            runtime_bindings=runtime_bindings,
        )
        raw_result = _run_stage(stage, config, stage_context)
        if raw_result.stage != stage or raw_result.status is not StageStatus.COMPLETE:
            raise RuntimeError("stage function returned a non-complete or mismatched result")
        if any(not isinstance(name, str) for name in raw_result.outputs):
            raise StageContractError(f"{stage} output allowlist keys must be strings")
        actual_outputs = {name: Path(path).resolve() for name, path in raw_result.outputs.items()}
        expected_outputs = {name: Path(path).resolve() for name, path in required_outputs.items()}
        if actual_outputs != expected_outputs:
            missing = sorted(set(expected_outputs) - set(actual_outputs))
            extra = sorted(set(actual_outputs) - set(expected_outputs))
            changed = {
                name: {"expected": str(expected_outputs[name]), "actual": str(actual_outputs[name])}
                for name in sorted(set(actual_outputs) & set(expected_outputs))
                if actual_outputs[name] != expected_outputs[name]
            }
            raise StageContractError(
                f"{stage} output allowlist differs: missing={missing}, extra={extra}, changed={changed}"
            )
        contract = validate_stage_contract(stage, config, required_outputs)
        if contract.get("status") != "passed":
            raise StageContractError(f"{stage} contract did not report passed status")
        result = StageResult(
            stage=stage,
            status=StageStatus.COMPLETE,
            outputs=required_outputs,
            metrics=raw_result.metrics,
            contract=contract,
        )
        complete_stage(
            stage_context,
            result,
            config_fingerprint=config.fingerprint,
            inputs_fingerprint=inputs_fingerprint,
        )
        return result
    except KeyboardInterrupt as error:
        _remove_success_marker(stage_context.stage_dir, error)
        try:
            end_stage_with_status(stage_context, StageStatus.INTERRUPTED)
        except Exception as manifest_error:
            error.add_note(f"could not record interrupted stage status: {manifest_error}")
        raise
    except Exception as error:
        _remove_success_marker(stage_context.stage_dir, error)
        try:
            end_stage_with_status(
                stage_context,
                StageStatus.FAILED,
                f"{type(error).__name__}: {error}",
            )
        except Exception as manifest_error:
            error.add_note(f"could not record failed stage status: {manifest_error}")
        raise


__all__ = [
    "RESOLVED_CONFIG_FILENAME",
    "ResumeConflictError",
    "RunConflictError",
    "StageContractError",
    "collect_stage_inputs",
    "default_run_id",
    "execute_stage",
    "formal_stage_outputs",
    "validate_stage_contract",
]
