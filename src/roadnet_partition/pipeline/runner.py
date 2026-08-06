from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
import traceback
from typing import Any, Mapping, TextIO

import yaml

from roadnet_partition import config as config_module
from roadnet_partition.config import (
    ConfigError,
    ResolvedStageConfig,
    config_fingerprint,
    resolve_demand_config,
    resolve_partition_config,
    resolve_supply_config,
    resolve_tte_config,
    stable_value,
)
from roadnet_partition.io.manifests import (
    MANIFEST_FILENAME,
    ProvenanceError,
    RUN_MANIFEST_SCHEMA_VERSION,
    atomic_write_json,
    atomic_write_yaml,
    collect_git_info,
    collect_runtime_info,
    evaluate_resume,
    file_record,
    initialize_run,
    input_fingerprint,
    invalidate_from_stage,
    load_manifest,
    provenance_mismatch_reasons,
    utc_now,
    validate_manifest,
    verify_run_ownership,
)
from roadnet_partition.io.paths import assert_owned_path, assert_safe_run_dir, resolve_path
from roadnet_partition.pipeline.results import PipelineResult, ResolvedPipelineConfig, RunContext, StageStatus
from roadnet_partition.pipeline.stages import (
    PIPELINE_BINDINGS,
    RUN_ID,
    STAGE_ORDER,
    ResumeConflictError,
    RunConflictError,
    canonical_partition_output_key,
    collect_stage_inputs,
    execute_stage,
    formal_stage_outputs,
)


_RESOLVERS = {
    "partition": resolve_partition_config,
    "demand": resolve_demand_config,
    "supply": resolve_supply_config,
    "tte": resolve_tte_config,
}
_SOURCE_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PIPELINE_SCHEMA = {
    "schema_version": config_module._ANY,
    "project_root": config_module._ANY,
    "scope": config_module._ANY,
    "run": {"root": config_module._ANY, "isolate_stages": config_module._ANY},
    "preparation": {"config": config_module._ANY},
    "stages": {
        stage: {"config": config_module._ANY, "required": config_module._ANY}
        for stage in STAGE_ORDER
    },
}


def resolve_pipeline_config(path: str | Path) -> ResolvedPipelineConfig:
    source, raw = config_module._load_yaml_mapping(path)
    config_module._validate_schema(raw, _PIPELINE_SCHEMA, source=source)
    if int(raw.get("schema_version", 0)) != 1:
        raise ConfigError(f"{source}: unsupported schema_version {raw.get('schema_version')!r}")
    if set(raw.get("stages", {})) != set(STAGE_ORDER):
        raise ConfigError(f"{source}: stages must be exactly {list(STAGE_ORDER)}")
    try:
        project_root = resolve_path(raw["project_root"], base_dir=source.parent)
        run_root = resolve_path(raw["run"]["root"], base_dir=source.parent)
    except (KeyError, ValueError) as error:
        raise ConfigError(f"{source}: invalid pipeline path: {error}") from error
    scope = config_module.validate_output_identifier(
        raw.get("scope"), source=source, field="pipeline scope",
    )
    isolation = raw.get("run", {}).get("isolate_stages")
    if not isinstance(isolation, bool):
        raise ConfigError(f"{source}: run.isolate_stages must be boolean")
    stages: dict[str, ResolvedStageConfig] = {}
    stage_values = {}
    for stage in STAGE_ORDER:
        entry = raw["stages"][stage]
        if not isinstance(entry.get("required"), bool):
            raise ConfigError(f"{source}: stages.{stage}.required must be boolean")
        config_path = resolve_path(entry["config"], base_dir=source.parent)
        if not config_path.is_file():
            raise ConfigError(f"{source}: stages.{stage}.config does not exist: {config_path}")
        resolved = _RESOLVERS[stage](config_path)
        if resolved.scope != scope:
            raise ConfigError(f"{source}: pipeline scope conflicts with {stage} scope")
        if resolved.project_root != project_root:
            raise ConfigError(f"{source}: pipeline project_root conflicts with {stage} project_root")
        stages[stage] = resolved
        stage_values[stage] = {
            "config_source": config_path.as_posix(),
            "source_fingerprint": resolved.fingerprint,
            "required": entry["required"],
        }
    preparation = raw.get("preparation")
    preparation_path = None
    if preparation is not None:
        preparation_path = resolve_path(preparation["config"], base_dir=source.parent)
        if not preparation_path.is_file():
            raise ConfigError(f"{source}: preparation.config does not exist: {preparation_path}")
    values = {
        "schema_version": 1,
        "project_root": project_root.as_posix(),
        "scope": scope,
        "run": {"root": run_root.as_posix(), "isolate_stages": isolation},
        "preparation": None if preparation_path is None else {"config": preparation_path.as_posix()},
        "stage_order": list(STAGE_ORDER),
        "stages": stage_values,
    }
    return ResolvedPipelineConfig(
        source, project_root, scope, run_root, isolation, stages, values, config_fingerprint(values),
    )


def default_pipeline_run_id(config: ResolvedPipelineConfig, fingerprint: str | None = None) -> str:
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{config.scope}-full-{(fingerprint or config.fingerprint)[:8]}"


def _validate_stage_range(from_stage: str, to_stage: str) -> tuple[int, int]:
    if from_stage not in STAGE_ORDER:
        raise ConfigError(f"unknown stage: {from_stage!r}")
    if to_stage not in STAGE_ORDER:
        raise ConfigError(f"unknown stage: {to_stage!r}")
    start, stop = STAGE_ORDER.index(from_stage), STAGE_ORDER.index(to_stage)
    if start > stop:
        raise ConfigError(f"from-stage {from_stage!r} is later than to-stage {to_stage!r}")
    return start, stop


def _external_inputs(
    config: ResolvedPipelineConfig,
    preparation_inputs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Mapping[str, Any]]:
    preparation = config.values.get("preparation")
    if preparation:
        from roadnet_partition.pipeline.preparation import input_records, load_config

        records = dict(preparation_inputs) if preparation_inputs is not None else input_records(
            load_config(Path(preparation["config"]), config.project_root),
        )
        demand = config.stages["demand"].values["order_pipeline"]["inputs"]
        records.update({
            **{f"demand.orders.{index}": file_record(path) for index, path in enumerate(demand["order_datasets"])},
            "demand.poi": file_record(demand["poi_path"]),
        })
        tte = config.stages["tte"].values["stage4_tte"]
        if tte["inputs"].get("network_distance_path"):
            records["tte.network_distance"] = file_record(tte["inputs"]["network_distance_path"])
            records["tte.representative_nodes"] = file_record(tte["inputs"]["representative_nodes_path"])
        else:
            records["tte.graphml"] = file_record(tte["distance"]["graphml_path"])
        return records
    records = {f"partition.{name}": record for name, record in collect_stage_inputs("partition", config.stages["partition"]).items()}
    demand = config.stages["demand"].values["order_pipeline"]["inputs"]
    records.update({
        "demand.road_relation_edges": file_record(demand["road_relation_edges_csv"]),
        "demand.poi": file_record(demand["poi_path"]),
        **{f"demand.orders.{index}": file_record(path) for index, path in enumerate(demand["order_datasets"])},
    })
    tte = config.stages["tte"].values["stage4_tte"]
    if tte["inputs"].get("network_distance_path"):
        records["tte.network_distance"] = file_record(tte["inputs"]["network_distance_path"])
        records["tte.representative_nodes"] = file_record(tte["inputs"]["representative_nodes_path"])
    else:
        records["tte.distance.graphml_path"] = file_record(tte["distance"]["graphml_path"])
        records["tte.distance.classified_edges_path"] = file_record(tte["distance"]["classified_edges_path"])
    return records


def _pipeline_values(
    config: ResolvedPipelineConfig,
    preparation_identity: Mapping[str, Any] | None,
    runtime: Mapping[str, Any] | None = None,
    git: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    values = deepcopy(dict(config.values))
    if preparation_identity is not None:
        values["preparation"] = {
            **dict(values["preparation"]),
            "identity": stable_value(preparation_identity),
        }
    if runtime is not None and git is not None:
        values["provenance"] = {
            "runtime_provenance_digest": runtime["digest"],
            "git_provenance_digest": git["digest"],
        }
    return values


def _pipeline_fingerprint(
    config: ResolvedPipelineConfig,
    preparation_identity: Mapping[str, Any] | None,
    runtime: Mapping[str, Any] | None = None,
    git: Mapping[str, Any] | None = None,
) -> str:
    return config_fingerprint(_pipeline_values(config, preparation_identity, runtime, git))


def _without_preparation_inputs(inputs: Mapping[str, Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {name: record for name, record in inputs.items() if not name.startswith("preparation.")}


def _with_preparation_outputs(
    config: ResolvedPipelineConfig,
    outputs: Mapping[str, Path],
) -> ResolvedPipelineConfig:
    stages = dict(config.stages)
    fields = {
        "partition": {
            "inputs.graph": outputs["graph"],
            "inputs.relation_edges": outputs["relation_edges"],
            "inputs.classified_edges": outputs["classified_edges"],
            "inputs.segment_nodes": outputs["segment_nodes"],
            "inputs.poi_features": outputs["poi_features"],
            "inputs.order_features": outputs["order_features"],
            "inputs.hourly_od": outputs["hourly_od"],
            "inputs.baseline_clusters.leiden": outputs["baseline_leiden"],
        },
        "demand": {
            "order_pipeline.inputs.road_relation_edges_csv": outputs["relation_edges"],
        },
        "tte": {
            "stage4_tte.distance.classified_edges_path": outputs["classified_edges"],
        },
    }
    for stage, replacements in fields.items():
        base = stages[stage]
        values = deepcopy(dict(base.values))
        for field, value in replacements.items():
            _set_field(values, field, str(value))
        stages[stage] = ResolvedStageConfig(
            base.source_path, values, config_fingerprint(values), base.stage,
            base.scope, base.project_root, base.dataset_path,
        )
    return ResolvedPipelineConfig(
        config.source_path, config.project_root, config.scope, config.run_root,
        config.isolate_stages, stages, config.values, config.fingerprint,
    )


def _write_manifest(context: RunContext, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    atomic_write_json(context.run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)


def _set_field(values: dict[str, Any], field: str, value: str) -> None:
    current = values
    parts = field.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _binding_specs(stage: str, config: ResolvedStageConfig) -> list[tuple[str, str, str, str]]:
    specs = list(PIPELINE_BINDINGS.get(stage, ()))
    if stage == "tte":
        inputs = config.values["stage4_tte"]["inputs"]
        if not inputs.get("network_distance_path"):
            specs.append(("partition", "canonical_partition", "stage4_tte.distance.partition_gpkg", "distance.partition_gpkg"))
    return specs


def _prepare_stage(
    pipeline: ResolvedPipelineConfig,
    context: RunContext,
    stage: str,
    cache: dict[str, tuple[ResolvedStageConfig, dict[str, Mapping[str, Any]], list[dict[str, Any]]]] | None = None,
    validated: set[str] | None = None,
) -> tuple[ResolvedStageConfig, dict[str, Mapping[str, Any]], list[dict[str, Any]]]:
    cache = {} if cache is None else cache
    validated = set() if validated is None else validated
    if stage in cache:
        return cache[stage]
    base = pipeline.stages[stage]
    values = deepcopy(dict(base.values))
    bindings = []
    bound_records = {}
    for producer, logical_key, field, consumer_input_key in _binding_specs(stage, base):
        producer_config, producer_inputs, _ = _prepare_stage(pipeline, context, producer, cache, validated)
        if producer not in validated:
            _validate_completed_stage(context, producer, producer_config, producer_inputs)
            validated.add(producer)
        manifest = load_manifest(context.run_dir)
        outputs = manifest["stages"][producer]["outputs"]
        actual_key = canonical_partition_output_key(outputs) if logical_key == "canonical_partition" else logical_key
        record = outputs.get(actual_key)
        if not isinstance(record, dict):
            raise ResumeConflictError(f"{producer} output {actual_key!r} is absent from its allowlist")
        fallback = _field_value(values, field)
        _set_field(values, field, record["path"])
        binding = {
            "producer_run_id": context.run_id,
            "producer_stage": producer,
            "producer_logical_key": actual_key,
            "consumer_stage": stage,
            "consumer_config_field": field,
            "consumer_input_key": consumer_input_key,
            "path": record["path"],
            "size": record["size"],
            "sha256": record["sha256"],
            "standalone_fallback": fallback,
            "pipeline_binding_wins": True,
        }
        bindings.append(binding)
        bound_records[consumer_input_key] = binding
    values["_resolved"] = {
        **dict(values.get("_resolved", {})),
        "source_config_path": base.source_path.as_posix(),
        "runtime_bindings": bindings,
    }
    resolved = ResolvedStageConfig(
        base.source_path, values, config_fingerprint(values), base.stage, base.scope,
        base.project_root, base.dataset_path,
    )
    inputs = collect_stage_inputs(stage, resolved)
    for name, binding in bound_records.items():
        actual = inputs[name]
        if actual["sha256"] != binding["sha256"] or actual["path"] != binding["path"]:
            raise ResumeConflictError(f"binding hash/path changed while preparing {stage}.{name}")
        inputs[name] = {
            **actual,
            "producer_stage": binding["producer_stage"],
            "producer_logical_key": binding["producer_logical_key"],
            "consumer_config_field": binding["consumer_config_field"],
        }
    prepared = resolved, inputs, bindings
    cache[stage] = prepared
    return prepared


def _field_value(values: Mapping[str, Any], field: str) -> Any:
    current: Any = values
    for part in field.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return stable_value(current)


def _validate_completed_stage(
    context: RunContext,
    stage: str,
    config: ResolvedStageConfig,
    inputs: Mapping[str, Mapping[str, Any]],
) -> None:
    decision = evaluate_resume(
        context.for_stage(stage),
        config_fingerprint=config.fingerprint,
        inputs_fingerprint=input_fingerprint(inputs),
        required_outputs=formal_stage_outputs(stage, config, context.for_stage(stage).stage_dir),
        require_run_complete=False,
    )
    if not decision.reusable:
        raise ResumeConflictError(f"upstream {stage} is not reusable: {'; '.join(decision.reasons)}")


def _stage_snapshot(
    config: ResolvedStageConfig,
    bindings: list[dict[str, Any]],
    *,
    requested_from: str,
    requested_to: str,
    isolate_stages: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_path": config.source_path.as_posix(),
        "dataset_path": None if config.dataset_path is None else config.dataset_path.as_posix(),
        "fingerprint": config.fingerprint,
        "runtime_bindings": bindings,
        "pipeline_invocation": {
            "requested_from": requested_from,
            "requested_to": requested_to,
            "isolate_stages": isolate_stages,
        },
        "resolved": stable_value(config.values),
    }


class _Tee:
    def __init__(self, stream: TextIO, log: TextIO) -> None:
        self.stream, self.log = stream, log

    def write(self, value: str) -> int:
        self.stream.write(value)
        self.stream.flush()
        self.log.write(value)
        self.log.flush()
        return len(value)

    def flush(self) -> None:
        self.stream.flush()
        self.log.flush()


def _run_direct(
    context: RunContext,
    stage: str,
    config: ResolvedStageConfig,
    inputs: Mapping[str, Mapping[str, Any]],
    stdout_path: Path,
    stderr_path: Path,
    bindings: list[Mapping[str, Any]],
) -> int:
    try:
        with stdout_path.open("a", encoding="utf-8") as stdout_log, stderr_path.open("a", encoding="utf-8") as stderr_log:
            with redirect_stdout(_Tee(sys.stdout, stdout_log)), redirect_stderr(_Tee(sys.stderr, stderr_log)):
                try:
                    execute_stage(
                        stage=stage, config=config, resume=True,
                        prepared_run_context=context, prepared_inputs=inputs,
                        runtime_bindings=bindings,
                    )
                except BaseException as error:
                    traceback.print_exc()
                    _record_execution(
                        context, stage, 130 if isinstance(error, KeyboardInterrupt) else 1,
                        stdout_path, stderr_path, "direct",
                    )
                    raise
    except OSError as error:
        if load_manifest(context.run_dir)["stages"].get(stage, {}).get("status") == StageStatus.NOT_STARTED.value:
            _record_launch_failure(context, stage, error)
            _record_execution(context, stage, None, stdout_path, stderr_path, "direct")
        raise
    return 0


def _pump(source: TextIO, console: TextIO, log: TextIO) -> None:
    for line in iter(source.readline, ""):
        console.write(line)
        console.flush()
        log.write(line)
        log.flush()


def _run_isolated(
    context: RunContext,
    stage: str,
    request_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> int:
    command = [
        sys.executable, "-m", "roadnet_partition.pipeline.worker",
        "--run-dir", str(context.run_dir), "--stage", stage, "--request", str(request_path),
    ]
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as error:
        _record_launch_failure(context, stage, error)
        _record_execution(context, stage, None, stdout_path, stderr_path, "isolated")
        raise
    interrupted = {"signal": None}
    previous = {}

    def forward(signum: int, _frame: Any) -> None:
        interrupted["signal"] = signum
        if child.poll() is None:
            child.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, forward)
    exit_code = -1
    try:
        with stdout_path.open("a", encoding="utf-8") as stdout_log, stderr_path.open("a", encoding="utf-8") as stderr_log:
            threads = [
                threading.Thread(target=_pump, args=(child.stdout, sys.stdout, stdout_log), daemon=True),
                threading.Thread(target=_pump, args=(child.stderr, sys.stderr, stderr_log), daemon=True),
            ]
            for thread in threads:
                thread.start()
            exit_code = child.wait()
            for thread in threads:
                thread.join()
    except BaseException:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                exit_code = child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                exit_code = child.wait()
        _normalize_running(context, stage, exit_code, "parent logging/forwarding failed")
        raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    _record_execution(context, stage, exit_code, stdout_path, stderr_path, "isolated")
    if interrupted["signal"] is not None:
        _normalize_running(context, stage, exit_code, "interrupted by parent signal")
        raise KeyboardInterrupt
    if exit_code:
        _normalize_running(context, stage, exit_code, "worker exited before completing manifest update")
        raise RunConflictError(f"{stage} worker exited with code {exit_code}")
    return exit_code


def _record_launch_failure(context: RunContext, stage: str, error: BaseException) -> None:
    manifest = verify_run_ownership(context)
    record = manifest["stages"].setdefault(stage, {})
    record.update({"status": StageStatus.FAILED.value, "error": f"{type(error).__name__}: {error}", "ended_at": utc_now()})
    manifest["status"] = StageStatus.FAILED.value
    _write_manifest(context, manifest)


def _normalize_running(context: RunContext, stage: str, exit_code: int, error: str) -> None:
    manifest = verify_run_ownership(context)
    record = manifest["stages"].get(stage, {})
    if record.get("status") == StageStatus.RUNNING.value:
        record.update({"status": StageStatus.INTERRUPTED.value, "error": error, "ended_at": utc_now()})
        manifest["status"] = StageStatus.INTERRUPTED.value
        _write_manifest(context, manifest)
    _record_execution(context, stage, exit_code, context.run_dir / f"logs/{stage}.stdout.log", context.run_dir / f"logs/{stage}.stderr.log", "isolated")


def _record_execution(context: RunContext, stage: str, exit_code: int | None, stdout: Path, stderr: Path, mode: str) -> None:
    manifest = verify_run_ownership(context)
    record = manifest["stages"].setdefault(stage, {})
    record["execution"] = {
        "mode": mode,
        "exit_code": exit_code,
        "stdout_log": stdout.relative_to(context.run_dir).as_posix(),
        "stderr_log": stderr.relative_to(context.run_dir).as_posix(),
    }
    _write_manifest(context, manifest)


def _pipeline_completion(manifest: Mapping[str, Any], required: Mapping[str, bool]) -> tuple[str | None, bool]:
    completed = None
    for stage in STAGE_ORDER:
        if manifest["stages"].get(stage, {}).get("status") != StageStatus.COMPLETE.value:
            break
        completed = stage
    all_required = all(
        not required[stage] or manifest["stages"].get(stage, {}).get("status") == StageStatus.COMPLETE.value
        for stage in STAGE_ORDER
    )
    return completed, all_required


def run_pipeline(
    config: ResolvedPipelineConfig,
    *,
    run_id: str | None = None,
    run_dir: Path | None = None,
    from_stage: str = "partition",
    to_stage: str = "tte",
    resume: bool = False,
    overwrite: bool = False,
    isolate_stages: bool | None = None,
    allow_dirty: bool = False,
) -> PipelineResult:
    if resume and overwrite:
        raise RunConflictError("--resume and --overwrite are mutually exclusive")
    start, stop = _validate_stage_range(from_stage, to_stage)
    if run_id is not None and (not RUN_ID.fullmatch(run_id) or run_id in {".", ".."}):
        raise ConfigError(f"invalid run ID: {run_id!r}")
    selected_isolation = config.isolate_stages if isolate_stages is None else isolate_stages
    preparation = config.values.get("preparation")
    preparation_inspection = None
    preparation_identity = None
    preparation_identity_state = None
    if preparation:
        from roadnet_partition.pipeline.preparation import preparation_identity as calculate_preparation_identity

        preparation_identity_state = calculate_preparation_identity(
            Path(preparation["config"]), config.project_root,
        )
        _, preparation_identity = preparation_identity_state
    runtime_provenance = collect_runtime_info()
    try:
        git_provenance = collect_git_info(config.project_root)
    except ProvenanceError as error:
        raise RunConflictError(f"Git provenance rejected before run creation ({error.reason})") from error
    if git_provenance.get("dirty") is True and not allow_dirty:
        raise RunConflictError("dirty Git worktree requires --allow-dirty")
    pipeline_fingerprint = _pipeline_fingerprint(config, preparation_identity, runtime_provenance, git_provenance)
    pipeline_values = _pipeline_values(config, preparation_identity, runtime_provenance, git_provenance)
    generated_id = run_id or default_pipeline_run_id(config, pipeline_fingerprint)
    destination = Path(run_dir) if run_dir is not None else config.run_root / generated_id
    protected_roots = {
        Path(value)
        for stage in config.stages.values()
        for value in stage.values.get("_resolved", {}).get("dataset_roots", [])
    }
    destination = assert_safe_run_dir(
        destination,
        config.project_root,
        additional_project_roots=(_SOURCE_PROJECT_ROOT,),
        protected_roots=protected_roots,
    )
    if preparation:
        from roadnet_partition.pipeline.preparation import inspect_resume

        preparation_inspection = inspect_resume(
            Path(preparation["config"]), config.project_root, destination / "preparation",
            identity_state=preparation_identity_state,
        )
        preparation_identity = preparation_inspection["current_identity"]
        pipeline_fingerprint = _pipeline_fingerprint(config, preparation_identity, runtime_provenance, git_provenance)
        pipeline_values = _pipeline_values(config, preparation_identity, runtime_provenance, git_provenance)
    existed = destination.exists() and any(destination.iterdir())
    external_inputs = _external_inputs(
        config,
        None if preparation_identity is None else preparation_identity["inputs"],
    )
    execution_start = start
    preparation_reason = None if preparation_inspection is None else preparation_inspection["reason"]
    invalidated_stages: list[str] = []
    if not existed:
        if resume or overwrite:
            raise ResumeConflictError(f"cannot resume/overwrite a missing pipeline run: {destination}")
        if from_stage != "partition":
            raise RunConflictError("a new pipeline run must start from partition")
        context = initialize_run(
            destination, run_id=generated_id, scope=config.scope, project_root=config.project_root,
            config_source=config.source_path, config_values=pipeline_values,
            config_fingerprint=pipeline_fingerprint, inputs=external_inputs,
            runtime=runtime_provenance, git=git_provenance,
            base_config_fingerprint=config.fingerprint,
            preparation_identity_digest=None if preparation_identity is None else preparation_identity["digest"],
        )
        manifest = load_manifest(destination)
        manifest.update({
            "run_kind": "pipeline",
            "pipeline": {
                "config_source": config.source_path.as_posix(),
                "base_config_fingerprint": config.fingerprint,
                "config_fingerprint": pipeline_fingerprint,
                "stage_order": list(STAGE_ORDER),
                "stage_config_sources": {stage: config.stages[stage].source_path.as_posix() for stage in STAGE_ORDER},
                "requested_from": from_stage,
                "requested_to": to_stage,
                "isolate_stages": selected_isolation,
                "completed_through": None,
                "all_required_stages_complete": False,
                "invocation_history": [],
                "preparation": None if preparation_inspection is None else {
                    "identity": preparation_identity,
                    "last_decision": {
                        "reason": preparation_reason,
                        "stored_identity": preparation_inspection["stored_identity"],
                        "current_identity": preparation_identity,
                        "invalidated_stages": [],
                        "recomputation_required": not preparation_inspection["reusable"],
                        "recomputation_performed": False,
                    },
                },
            },
        })
        manifest["stages"] = {stage: {"status": "not_started", "directory": stage} for stage in STAGE_ORDER}
        _write_manifest(context, manifest)
    else:
        if not (resume or overwrite):
            raise RunConflictError(f"pipeline run already exists; use --resume or --overwrite: {destination}")
        marker = json.loads((destination / ".roadnet-run").read_text(encoding="utf-8"))
        if run_id is not None and marker.get("run_id") != run_id:
            raise RunConflictError("requested run ID differs from owned run ID")
        context = RunContext(str(marker["run_id"]), destination, config.project_root, log_dir=destination / "logs")
        manifest = verify_run_ownership(context)
        if manifest.get("run_kind") != "pipeline":
            raise RunConflictError("existing run is not a pipeline run")
        stored_base_fingerprint = manifest["pipeline"].get(
            "base_config_fingerprint", manifest["config"]["fingerprint"],
        )
        if stored_base_fingerprint != config.fingerprint:
            raise ResumeConflictError("pipeline config fingerprint changed; use a new run ID")
        stored_inputs = manifest.get("inputs", {}).get("files", {})
        if not isinstance(stored_inputs, Mapping) or _without_preparation_inputs(stored_inputs) != _without_preparation_inputs(external_inputs):
            raise ResumeConflictError("pipeline external input fingerprint changed; use a new run ID")
        provenance_decision = provenance_mismatch_reasons(
            manifest.get("runtime"), runtime_provenance, manifest.get("git"), git_provenance,
        )
        if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA_VERSION:
            provenance_decision = {
                "runtime": ["runtime_provenance_missing_legacy_manifest"],
                "git": ["git_provenance_missing_legacy_manifest"],
                "compatible": False,
            }
        provenance_decision.update({
            "stored": {
                "runtime_provenance_digest": manifest.get("runtime", {}).get("digest"),
                "git_provenance_digest": manifest.get("git", {}).get("digest"),
            },
            "current": {
                "runtime_provenance_digest": runtime_provenance["digest"],
                "git_provenance_digest": git_provenance["digest"],
            },
            "invalidated_stages": [] if provenance_decision["compatible"] else list(STAGE_ORDER),
            "recomputation_required": not provenance_decision["compatible"],
            "recomputation_performed": False,
        })
        if not provenance_decision["compatible"]:
            invalidated_stages = list(STAGE_ORDER)
            invalidate_from_stage(context, ordered_stages=list(STAGE_ORDER), from_stage="partition")
            execution_start = 0
            if preparation_inspection is not None:
                preparation_inspection = {
                    **preparation_inspection,
                    "reusable": False,
                    "reason": next(
                        reason for reason in provenance_decision["runtime"] + provenance_decision["git"]
                        if not reason.endswith("_match")
                    ),
                    "has_state": True,
                }
                preparation_reason = preparation_inspection["reason"]
        if preparation_inspection is not None:
            from roadnet_partition.pipeline.preparation import identity_mismatch_reason

            stored_preparation = manifest["pipeline"].get("preparation")
            stored_identity = stored_preparation.get("identity") if isinstance(stored_preparation, dict) else None
            parent_reason = identity_mismatch_reason(stored_identity, preparation_identity)
            if parent_reason != "preparation_identity_match":
                preparation_reason = parent_reason
                preparation_inspection = {
                    **preparation_inspection,
                    "reusable": False,
                    "reason": parent_reason,
                    "has_state": True,
                    "stored_identity": stored_identity,
                }
            if not preparation_inspection["reusable"] and not invalidated_stages:
                invalidated_stages = list(STAGE_ORDER)
                invalidate_from_stage(context, ordered_stages=list(STAGE_ORDER), from_stage="partition")
                execution_start = 0
        if overwrite and not invalidated_stages:
            invalidate_from_stage(context, ordered_stages=list(STAGE_ORDER), from_stage=from_stage)
        manifest = verify_run_ownership(context)
        manifest["schema_version"] = RUN_MANIFEST_SCHEMA_VERSION
        manifest["runtime"] = stable_value(runtime_provenance)
        manifest["git"] = stable_value(git_provenance)
        manifest["runtime_provenance_complete"] = True
        manifest["git_provenance_complete"] = bool(git_provenance.get("complete"))
        manifest["experiment"] = {
            "schema_version": 1,
            "base_config_fingerprint": config.fingerprint,
            "preparation_identity_digest": None if preparation_identity is None else preparation_identity["digest"],
            "runtime_provenance_digest": runtime_provenance["digest"],
            "git_provenance_digest": git_provenance["digest"],
            "fingerprint": pipeline_fingerprint,
        }
        manifest["config"]["fingerprint"] = pipeline_fingerprint
        manifest["config"]["resolved"] = stable_value(pipeline_values)
        manifest["inputs"] = {
            "fingerprint": input_fingerprint(external_inputs),
            "files": stable_value(external_inputs),
        }
        manifest["pipeline"].update({
            "base_config_fingerprint": config.fingerprint,
            "config_fingerprint": pipeline_fingerprint,
            "last_provenance_decision": provenance_decision,
        })
        if preparation_inspection is not None:
            manifest["pipeline"]["preparation"] = {
                "identity": preparation_identity,
                "last_decision": {
                    "reason": preparation_reason,
                    "stored_identity": preparation_inspection["stored_identity"],
                    "current_identity": preparation_identity,
                    "invalidated_stages": invalidated_stages,
                    "recomputation_required": not preparation_inspection["reusable"],
                    "recomputation_performed": False,
                },
            }
        _write_manifest(context, manifest)
    manifest = verify_run_ownership(context)
    invocation = {
        "started_at": utc_now(), "requested_from": from_stage, "requested_to": to_stage,
        "isolate_stages": selected_isolation, "mode": "overwrite" if overwrite else "resume" if resume else "new",
    }
    manifest["pipeline"]["invocation_history"].append(invocation)
    manifest["pipeline"].update({"requested_from": from_stage, "requested_to": to_stage, "isolate_stages": selected_isolation})
    _write_manifest(context, manifest)
    atomic_write_yaml(destination / "resolved_config.yaml", {
        "schema_version": 1, "source_path": config.source_path.as_posix(),
        "fingerprint": pipeline_fingerprint, "requested_from": from_stage,
        "requested_to": to_stage, "isolate_stages": selected_isolation,
        "resolved": pipeline_values,
    })
    (destination / "resolved_configs").mkdir(exist_ok=True)
    (destination / "requests").mkdir(exist_ok=True)

    active_stage: str | None = None
    try:
        runtime_config = config
        if preparation:
            from roadnet_partition.pipeline.preparation import run as run_preparation

            if not preparation_inspection["reusable"]:
                manifest = verify_run_ownership(context)
                manifest["pipeline"]["preparation"]["last_decision"]["recomputation_performed"] = True
                _write_manifest(context, manifest)
            prepared_outputs = run_preparation(
                Path(preparation["config"]), config.project_root, destination / "preparation",
                inspection=preparation_inspection,
            )
            runtime_config = _with_preparation_outputs(config, prepared_outputs)
            manifest = verify_run_ownership(context)
            manifest["pipeline"]["preparation"]["identity"] = preparation_identity
            _write_manifest(context, manifest)
        for upstream in STAGE_ORDER[:execution_start]:
            upstream_config, upstream_inputs, _ = _prepare_stage(runtime_config, context, upstream)
            _validate_completed_stage(context, upstream, upstream_config, upstream_inputs)
        for stage in STAGE_ORDER[execution_start:stop + 1]:
            active_stage = stage
            resolved, inputs, bindings = _prepare_stage(runtime_config, context, stage)
            snapshot_path = assert_owned_path(destination / "resolved_configs" / f"{stage}.yaml", destination)
            atomic_write_yaml(snapshot_path, _stage_snapshot(
                resolved, bindings,
                requested_from=from_stage,
                requested_to=to_stage,
                isolate_stages=selected_isolation,
            ))
            record = load_manifest(destination)["stages"].get(stage, {})
            if record.get("status") == StageStatus.COMPLETE.value:
                _validate_completed_stage(context, stage, resolved, inputs)
                print(f"{stage}: reused", flush=True)
                continue
            if record.get("status") in {StageStatus.FAILED.value, StageStatus.INTERRUPTED.value}:
                invalidate_from_stage(context, ordered_stages=list(STAGE_ORDER), from_stage=stage)
            stdout_path = destination / "logs" / f"{stage}.stdout.log"
            stderr_path = destination / "logs" / f"{stage}.stderr.log"
            if selected_isolation:
                request_path = assert_owned_path(destination / "requests" / f"{stage}.json", destination)
                atomic_write_json(request_path, {
                    "schema_version": 1, "run_id": context.run_id, "stage": stage,
                    "run_dir": destination.as_posix(), "resolved_config_path": snapshot_path.as_posix(),
                    "expected_config_fingerprint": resolved.fingerprint,
                    "binding_summary": bindings,
                })
                _run_isolated(context, stage, request_path, stdout_path, stderr_path)
            else:
                _run_direct(context, stage, resolved, inputs, stdout_path, stderr_path, bindings)
                _record_execution(context, stage, 0, stdout_path, stderr_path, "direct")
            print(f"{stage}: complete", flush=True)
        manifest = verify_run_ownership(context)
        decision = manifest["pipeline"].get("last_provenance_decision")
        if isinstance(decision, dict) and decision.get("recomputation_required"):
            decision["recomputation_performed"] = True
        completed, all_required = _pipeline_completion(
            manifest, {stage: bool(config.values["stages"][stage]["required"]) for stage in STAGE_ORDER},
        )
        manifest["status"] = StageStatus.COMPLETE.value
        manifest["pipeline"].update({"completed_through": completed, "all_required_stages_complete": all_required})
        invocation.update({"finished_at": utc_now(), "status": "complete", "completed_through": completed})
        manifest["pipeline"]["invocation_history"][-1] = invocation
        _write_manifest(context, manifest)
        return PipelineResult(context.run_id, destination, from_stage, to_stage, completed, all_required)
    except BaseException as error:
        manifest = verify_run_ownership(context)
        terminal = StageStatus.INTERRUPTED if isinstance(error, KeyboardInterrupt) else StageStatus.FAILED
        if active_stage is not None:
            record = manifest["stages"].setdefault(active_stage, {"directory": active_stage})
            if record.get("status") in {StageStatus.NOT_STARTED.value, StageStatus.RUNNING.value}:
                record.update({
                    "status": terminal.value,
                    "error": f"{type(error).__name__}: {error}",
                    "ended_at": utc_now(),
                })
        manifest["status"] = terminal.value
        invocation.update({"finished_at": utc_now(), "status": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"})
        manifest["pipeline"]["invocation_history"][-1] = invocation
        completed, all_required = _pipeline_completion(
            manifest, {stage: bool(config.values["stages"][stage]["required"]) for stage in STAGE_ORDER},
        )
        manifest["pipeline"].update({"completed_through": completed, "all_required_stages_complete": all_required})
        _write_manifest(context, manifest)
        raise


__all__ = ["default_pipeline_run_id", "resolve_pipeline_config", "run_pipeline"]
