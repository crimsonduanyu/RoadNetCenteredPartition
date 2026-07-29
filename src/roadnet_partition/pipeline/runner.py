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
    atomic_write_json,
    atomic_write_yaml,
    evaluate_resume,
    file_record,
    initialize_run,
    input_fingerprint,
    invalidate_from_stage,
    load_manifest,
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
    scope = str(raw.get("scope", ""))
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
            raise ConfigError(f"{source}: pipeline scope {scope!r} conflicts with {stage} scope {resolved.scope!r}")
        if resolved.project_root != project_root:
            raise ConfigError(f"{source}: pipeline project_root conflicts with {stage} project_root")
        stages[stage] = resolved
        stage_values[stage] = {
            "config_source": config_path.as_posix(),
            "source_fingerprint": resolved.fingerprint,
            "required": entry["required"],
        }
    values = {
        "schema_version": 1,
        "project_root": project_root.as_posix(),
        "scope": scope,
        "run": {"root": run_root.as_posix(), "isolate_stages": isolation},
        "stage_order": list(STAGE_ORDER),
        "stages": stage_values,
    }
    return ResolvedPipelineConfig(
        source, project_root, scope, run_root, isolation, stages, values, config_fingerprint(values),
    )


def default_pipeline_run_id(config: ResolvedPipelineConfig) -> str:
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{config.scope}-full-{config.fingerprint[:8]}"


def _validate_stage_range(from_stage: str, to_stage: str) -> tuple[int, int]:
    if from_stage not in STAGE_ORDER:
        raise ConfigError(f"unknown stage: {from_stage!r}")
    if to_stage not in STAGE_ORDER:
        raise ConfigError(f"unknown stage: {to_stage!r}")
    start, stop = STAGE_ORDER.index(from_stage), STAGE_ORDER.index(to_stage)
    if start > stop:
        raise ConfigError(f"from-stage {from_stage!r} is later than to-stage {to_stage!r}")
    return start, stop


def _external_inputs(config: ResolvedPipelineConfig) -> dict[str, Mapping[str, Any]]:
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
) -> PipelineResult:
    if resume and overwrite:
        raise RunConflictError("--resume and --overwrite are mutually exclusive")
    start, stop = _validate_stage_range(from_stage, to_stage)
    if run_id is not None and (not RUN_ID.fullmatch(run_id) or run_id in {".", ".."}):
        raise ConfigError(f"invalid run ID: {run_id!r}")
    selected_isolation = config.isolate_stages if isolate_stages is None else isolate_stages
    generated_id = run_id or default_pipeline_run_id(config)
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
    existed = destination.exists() and any(destination.iterdir())
    external_inputs = _external_inputs(config)
    if not existed:
        if resume or overwrite:
            raise ResumeConflictError(f"cannot resume/overwrite a missing pipeline run: {destination}")
        if from_stage != "partition":
            raise RunConflictError("a new pipeline run must start from partition")
        context = initialize_run(
            destination, run_id=generated_id, scope=config.scope, project_root=config.project_root,
            config_source=config.source_path, config_values=config.values,
            config_fingerprint=config.fingerprint, inputs=external_inputs,
        )
        manifest = load_manifest(destination)
        manifest.update({
            "run_kind": "pipeline",
            "pipeline": {
                "config_source": config.source_path.as_posix(),
                "config_fingerprint": config.fingerprint,
                "stage_order": list(STAGE_ORDER),
                "stage_config_sources": {stage: config.stages[stage].source_path.as_posix() for stage in STAGE_ORDER},
                "requested_from": from_stage,
                "requested_to": to_stage,
                "isolate_stages": selected_isolation,
                "completed_through": None,
                "all_required_stages_complete": False,
                "invocation_history": [],
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
        if manifest["config"]["fingerprint"] != config.fingerprint:
            raise ResumeConflictError("pipeline config fingerprint changed; use a new run ID")
        if manifest["inputs"]["fingerprint"] != input_fingerprint(external_inputs):
            raise ResumeConflictError("pipeline external input fingerprint changed; use a new run ID")
        if overwrite:
            invalidate_from_stage(context, ordered_stages=list(STAGE_ORDER), from_stage=from_stage)
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
        "fingerprint": config.fingerprint, "requested_from": from_stage,
        "requested_to": to_stage, "isolate_stages": selected_isolation,
        "resolved": config.values,
    })
    (destination / "resolved_configs").mkdir(exist_ok=True)
    (destination / "requests").mkdir(exist_ok=True)

    active_stage: str | None = None
    try:
        for upstream in STAGE_ORDER[:start]:
            upstream_config, upstream_inputs, _ = _prepare_stage(config, context, upstream)
            _validate_completed_stage(context, upstream, upstream_config, upstream_inputs)
        for stage in STAGE_ORDER[start:stop + 1]:
            active_stage = stage
            resolved, inputs, bindings = _prepare_stage(config, context, stage)
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
