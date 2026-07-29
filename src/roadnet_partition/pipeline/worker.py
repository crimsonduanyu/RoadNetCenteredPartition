from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
from typing import Any, Mapping

import yaml

from roadnet_partition.config import ResolvedStageConfig, config_fingerprint
from roadnet_partition.io.manifests import RUN_MARKER, load_manifest, verify_run_ownership
from roadnet_partition.io.paths import assert_owned_path
from roadnet_partition.pipeline.results import RunContext, StageStatus
from roadnet_partition.pipeline.stages import STAGE_ORDER, collect_stage_inputs, execute_stage


_REQUEST_FIELDS = {
    "schema_version", "run_id", "stage", "run_dir", "resolved_config_path",
    "expected_config_fingerprint", "binding_summary",
}


def _load_request(path: Path, run_dir: Path, stage: str) -> tuple[dict[str, Any], RunContext]:
    if stage not in STAGE_ORDER:
        raise ValueError(f"invalid worker stage: {stage!r}")
    owned = assert_owned_path(path, run_dir)
    if owned.parent != run_dir.resolve() / "requests":
        raise ValueError("worker request is outside the run requests directory")
    if path.is_symlink():
        raise ValueError("worker request may not be a symbolic link")
    request = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS or request.get("schema_version") != 1:
        raise ValueError("invalid worker request schema")
    if Path(request["run_dir"]).resolve() != run_dir.resolve() or request["stage"] != stage:
        raise ValueError("worker request target differs from invocation")
    marker_path = run_dir / RUN_MARKER
    if marker_path.is_symlink():
        raise ValueError("run marker may not be a symbolic link")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("run_id") != request["run_id"]:
        raise ValueError("worker request run ID differs from marker")
    manifest = load_manifest(run_dir)
    project_root = Path(manifest["config"]["resolved"]["project_root"])
    context = RunContext(str(request["run_id"]), run_dir.resolve(), project_root.resolve(), log_dir=run_dir / "logs")
    verify_run_ownership(context)
    expected_snapshot = assert_owned_path(run_dir / "resolved_configs" / f"{stage}.yaml", run_dir)
    supplied_snapshot = assert_owned_path(Path(request["resolved_config_path"]), run_dir)
    if supplied_snapshot != expected_snapshot or supplied_snapshot.is_symlink():
        raise ValueError("worker resolved config path differs from the owned stage snapshot")
    status = manifest["stages"].get(stage, {}).get("status")
    if status not in {StageStatus.NOT_STARTED.value, StageStatus.FAILED.value, StageStatus.INTERRUPTED.value}:
        raise ValueError(f"worker stage cannot start from status {status!r}")
    record = manifest["stages"].get(stage, {})
    if record.get("directory") != stage:
        raise ValueError("worker stage directory differs from manifest ownership")
    assert_owned_path(run_dir / stage, run_dir)
    return request, context


def _load_config(
    request: Mapping[str, Any],
    context: RunContext,
) -> tuple[ResolvedStageConfig, dict[str, dict[str, Any]]]:
    snapshot = yaml.safe_load(Path(request["resolved_config_path"]).read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("invalid resolved stage snapshot")
    values = snapshot.get("resolved")
    if not isinstance(values, dict):
        raise ValueError("resolved stage snapshot has no resolved mapping")
    fingerprint = config_fingerprint(values)
    if fingerprint != snapshot.get("fingerprint") or fingerprint != request["expected_config_fingerprint"]:
        raise ValueError("resolved config hash mismatch")
    bindings = request.get("binding_summary")
    if not isinstance(bindings, list) or bindings != snapshot.get("runtime_bindings"):
        raise ValueError("worker binding summary differs from resolved config snapshot")
    stage = str(request["stage"])
    config = ResolvedStageConfig(
        Path(snapshot["source_path"]), values, fingerprint, stage,
        str(values["_resolved"]["scope"]), context.project_root,
        None if snapshot.get("dataset_path") is None else Path(snapshot["dataset_path"]),
    )
    actual_inputs = collect_stage_inputs(stage, config)
    manifest = load_manifest(context.run_dir)
    for binding in bindings:
        if binding.get("producer_run_id") != context.run_id or binding.get("consumer_stage") != stage:
            raise ValueError("worker binding run/stage provenance differs")
        producer = binding.get("producer_stage")
        key = binding.get("producer_logical_key")
        recorded = manifest["stages"].get(producer, {}).get("outputs", {}).get(key)
        if not isinstance(recorded, dict) or any(binding.get(field) != recorded.get(field) for field in ("path", "size", "sha256")):
            raise ValueError("worker binding provenance differs from producer manifest")
        input_key = binding.get("consumer_input_key")
        actual = actual_inputs.get(input_key)
        if not isinstance(actual, dict) or any(actual.get(field) != recorded.get(field) for field in ("path", "size", "sha256")):
            raise ValueError("worker binding differs from resolved stage input")
        actual_inputs[input_key] = {
            **actual,
            "producer_stage": producer,
            "producer_logical_key": key,
            "consumer_config_field": binding.get("consumer_config_field"),
        }
    return config, actual_inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="roadnet-partition-pipeline-worker")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGE_ORDER, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    request, context = _load_request(args.request, args.run_dir, args.stage)
    config, inputs = _load_config(request, context)

    def interrupt(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    execute_stage(
        stage=str(request["stage"]), config=config, resume=True,
        prepared_run_context=context, prepared_inputs=inputs,
        runtime_bindings=request["binding_summary"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
