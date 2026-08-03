from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Mapping

import yaml

from roadnet_partition import __version__
from roadnet_partition.config import stable_value
from roadnet_partition.io.paths import assert_owned_path, assert_safe_run_dir
from roadnet_partition.pipeline.results import ResumeDecision, RunContext, StageResult, StageStatus


SCHEMA_VERSION = 1
RUN_MARKER = ".roadnet-run"
MANIFEST_FILENAME = "manifest.json"
SUCCESS_MARKER = "_SUCCESS"
STAGE_RESULT_FILENAME = "stage_result.json"
DEPENDENCY_ALLOWLIST = (
    "geopandas",
    "duckdb",
    "networkx",
    "numpy",
    "pandas",
    "pyarrow",
    "pyyaml",
    "scipy",
    "shapely",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str | Path, *, block_size: int = 8 * 1024 * 1024, follow_symlinks: bool = False) -> str:
    source = Path(path)
    if source.is_symlink() and not follow_symlinks:
        raise ValueError(f"refusing to hash symbolic link: {source}")
    if not source.exists():
        raise FileNotFoundError(source)
    if not source.is_file():
        raise IsADirectoryError(source)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    validator: Callable[[Any], None] | None = None,
) -> None:
    destination = Path(path)
    payload = stable_value(value)
    _atomic_write(destination, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    loaded = json.loads(destination.read_text(encoding="utf-8"))
    if validator is not None:
        validator(loaded)


def atomic_write_yaml(path: str | Path, value: Any) -> None:
    destination = Path(path)
    payload = stable_value(value)
    _atomic_write(destination, yaml.safe_dump(payload, allow_unicode=True, sort_keys=True))
    yaml.safe_load(destination.read_text(encoding="utf-8"))


def atomic_write_text(path: str | Path, value: str) -> None:
    _atomic_write(Path(path), value)


def collect_runtime_info() -> dict[str, Any]:
    dependencies = {}
    for name in DEPENDENCY_ALLOWLIST:
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "roadnet_partition": __version__,
        "dependencies": dependencies,
    }


def collect_git_info(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve()

    def git(*args: str, binary: bool = False) -> bytes | str:
        completed = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True,
            text=not binary,
        )
        return completed.stdout

    try:
        commit = str(git("rev-parse", "HEAD")).strip()
        status = str(git("status", "--porcelain=v1", "--untracked-files=all"))
        diff = bytes(git("diff", "--binary", "HEAD", binary=True))
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None, "changed_files": [], "diff_sha256": None}
    changed = [line[3:] if len(line) > 3 else line for line in status.splitlines()]
    digest = hashlib.sha256(diff + b"\0" + status.encode("utf-8")).hexdigest()
    return {
        "commit": commit,
        "dirty": bool(status),
        "changed_files": changed,
        "diff_sha256": digest,
    }


def file_record(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    return {"path": source.as_posix(), "size": source.stat().st_size, "sha256": sha256_file(source)}


def input_fingerprint(inputs: Mapping[str, Mapping[str, Any]]) -> str:
    payload = json.dumps(stable_value(inputs), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    required = {
        "schema_version", "run_id", "scope", "status", "created_at", "updated_at",
        "git", "runtime", "config", "inputs", "stages", "publish_history",
    }
    missing = required - manifest.keys()
    if missing:
        raise ValueError(f"manifest missing fields: {sorted(missing)}")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported manifest schema: {manifest['schema_version']}")
    if manifest["status"] not in {status.value for status in StageStatus}:
        raise ValueError(f"invalid run status: {manifest['status']}")
    if not isinstance(manifest["stages"], dict) or not isinstance(manifest["publish_history"], list):
        raise ValueError("manifest stages/publish_history have invalid types")
    for stage, record in manifest["stages"].items():
        if record.get("status") not in {status.value for status in StageStatus}:
            raise ValueError(f"invalid status for stage {stage!r}")


def new_manifest(
    *,
    run_id: str,
    scope: str,
    project_root: Path,
    config_source: Path,
    config_values: Mapping[str, Any],
    config_fingerprint: str,
    inputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "scope": scope,
        "status": StageStatus.NOT_STARTED.value,
        "created_at": now,
        "updated_at": now,
        "git": collect_git_info(project_root),
        "runtime": collect_runtime_info(),
        "config": {
            "source_path": config_source.resolve().as_posix(),
            "fingerprint": config_fingerprint,
            "resolved": stable_value(config_values),
        },
        "inputs": {
            "fingerprint": input_fingerprint(inputs),
            "files": stable_value(inputs),
        },
        "stages": {},
        "publish_history": [],
    }


def initialize_run(
    run_dir: str | Path,
    *,
    run_id: str,
    scope: str,
    project_root: str | Path,
    config_source: str | Path,
    config_values: Mapping[str, Any],
    config_fingerprint: str,
    inputs: Mapping[str, Mapping[str, Any]],
) -> RunContext:
    root = Path(project_root).resolve()
    destination = assert_safe_run_dir(Path(run_dir), root)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"run directory is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "logs").mkdir()
    marker = {"schema_version": SCHEMA_VERSION, "run_id": run_id}
    atomic_write_json(destination / RUN_MARKER, marker)
    manifest = new_manifest(
        run_id=run_id,
        scope=scope,
        project_root=root,
        config_source=Path(config_source),
        config_values=config_values,
        config_fingerprint=config_fingerprint,
        inputs=inputs,
    )
    atomic_write_json(destination / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    return RunContext(run_id=run_id, run_dir=destination, project_root=root, log_dir=destination / "logs")


def load_manifest(run_dir: str | Path) -> dict[str, Any]:
    manifest = json.loads((Path(run_dir) / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    validate_manifest(manifest)
    return manifest


def verify_run_ownership(context: RunContext) -> dict[str, Any]:
    marker_path = context.run_dir / RUN_MARKER
    manifest_path = context.run_dir / MANIFEST_FILENAME
    if marker_path.is_symlink() or manifest_path.is_symlink():
        raise ValueError("run ownership files may not be symbolic links")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    manifest = load_manifest(context.run_dir)
    if marker != {"schema_version": SCHEMA_VERSION, "run_id": context.run_id}:
        raise ValueError("run marker does not match context")
    if manifest["run_id"] != context.run_id:
        raise ValueError("manifest run ID does not match context")
    return manifest


def _write_manifest(context: RunContext, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    atomic_write_json(context.run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)


def begin_stage(
    context: RunContext,
    *,
    config_fingerprint: str,
    inputs_fingerprint: str,
    inputs: Mapping[str, Mapping[str, Any]] | None = None,
    runtime_bindings: list[Mapping[str, Any]] | None = None,
) -> RunContext:
    if context.stage_name is None or context.stage_dir is None:
        raise ValueError("stage context required")
    manifest = verify_run_ownership(context)
    current = manifest["stages"].get(context.stage_name, {}).get("status", StageStatus.NOT_STARTED.value)
    if current not in {StageStatus.NOT_STARTED.value, StageStatus.FAILED.value, StageStatus.INTERRUPTED.value}:
        raise ValueError(f"stage cannot start from status {current!r}")
    context.stage_dir.mkdir(parents=True, exist_ok=True)
    manifest["stages"][context.stage_name] = {
        "status": StageStatus.RUNNING.value,
        "directory": context.stage_dir.relative_to(context.run_dir).as_posix(),
        "config_fingerprint": config_fingerprint,
        "input_fingerprint": inputs_fingerprint,
        "input_records": stable_value(inputs or {}),
        "runtime_bindings": stable_value(runtime_bindings or []),
        "started_at": utc_now(),
        "outputs": {},
        "metrics": {},
        "error": None,
    }
    manifest["status"] = StageStatus.RUNNING.value
    _write_manifest(context, manifest)
    return context


def complete_stage(
    context: RunContext,
    result: StageResult,
    *,
    config_fingerprint: str,
    inputs_fingerprint: str,
) -> None:
    if result.status is not StageStatus.COMPLETE or result.stage != context.stage_name:
        raise ValueError("complete_stage requires a matching COMPLETE result")
    manifest = verify_run_ownership(context)
    record = manifest["stages"].get(result.stage)
    if record is None or record["status"] != StageStatus.RUNNING.value:
        raise ValueError("stage is not running")
    outputs = {}
    for name, path in result.outputs.items():
        owned = assert_owned_path(path, context.stage_dir)
        outputs[name] = file_record(owned)
    stage_result = {
        "schema_version": SCHEMA_VERSION,
        "stage": result.stage,
        "status": result.status.value,
        "config_fingerprint": config_fingerprint,
        "input_fingerprint": inputs_fingerprint,
        "outputs": outputs,
        "metrics": stable_value(result.metrics),
        "contract": stable_value(result.contract),
    }
    atomic_write_json(context.stage_dir / STAGE_RESULT_FILENAME, stage_result)
    success = {
        "schema_version": SCHEMA_VERSION,
        "stage": result.stage,
        "config_fingerprint": config_fingerprint,
        "input_fingerprint": inputs_fingerprint,
        "outputs": outputs,
    }
    success_path = context.stage_dir / SUCCESS_MARKER
    atomic_write_json(success_path, success)
    record.update({
        "status": StageStatus.COMPLETE.value,
        "config_fingerprint": config_fingerprint,
        "input_fingerprint": inputs_fingerprint,
        "outputs": outputs,
        "metrics": stage_result["metrics"],
        "contract": stage_result["contract"],
        "completed_at": utc_now(),
        "error": None,
    })
    manifest["status"] = StageStatus.COMPLETE.value
    try:
        _write_manifest(context, manifest)
    except BaseException as error:
        try:
            success_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            error.add_note(f"could not remove incomplete success marker: {cleanup_error}")
        raise


def end_stage_with_status(context: RunContext, status: StageStatus, error: str | None = None) -> None:
    if status not in {StageStatus.FAILED, StageStatus.INTERRUPTED}:
        raise ValueError("only failed/interrupted terminal states are accepted")
    manifest = verify_run_ownership(context)
    record = manifest["stages"].get(context.stage_name or "")
    if record is None or record["status"] != StageStatus.RUNNING.value:
        raise ValueError("stage is not running")
    record.update({"status": status.value, "error": error, "ended_at": utc_now()})
    manifest["status"] = status.value
    _write_manifest(context, manifest)


def evaluate_resume(
    context: RunContext,
    *,
    config_fingerprint: str,
    inputs_fingerprint: str,
    required_outputs: Mapping[str, Path],
    require_run_complete: bool = True,
) -> ResumeDecision:
    if context.stage_name is None or context.stage_dir is None:
        return ResumeDecision(False, ("stage context required",))
    reasons = []
    try:
        manifest = verify_run_ownership(context)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        return ResumeDecision(False, (str(error),))
    if require_run_complete and manifest.get("status") != StageStatus.COMPLETE.value:
        reasons.append("manifest run is not complete")
    record = manifest["stages"].get(context.stage_name)
    if record is None or record.get("status") != StageStatus.COMPLETE.value:
        reasons.append("manifest stage is not complete")
    stage_dir = context.stage_dir
    expected_directory = stage_dir.relative_to(context.run_dir).as_posix()
    if record is not None and record.get("directory") != expected_directory:
        reasons.append("manifest stage directory differs")
    if record is not None and record.get("config_fingerprint") != config_fingerprint:
        reasons.append("config fingerprint changed")
    if record is not None and record.get("input_fingerprint") != inputs_fingerprint:
        reasons.append("input fingerprint changed")
    if record is not None and record.get("contract", {}).get("status") != "passed":
        reasons.append("manifest contract is not passed")
    required_names = set(required_outputs)
    recorded_outputs = record.get("outputs", {}) if record else {}
    if not isinstance(recorded_outputs, dict) or set(recorded_outputs) != required_names:
        reasons.append("manifest output allowlist differs")
        recorded_outputs = recorded_outputs if isinstance(recorded_outputs, dict) else {}

    documents = {
        "success marker": stage_dir / SUCCESS_MARKER,
        "stage result": stage_dir / STAGE_RESULT_FILENAME,
    }
    loaded_documents: dict[str, dict[str, Any] | None] = {}
    for label, path in documents.items():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            value = None
            reasons.append(f"{label} missing or invalid")
        if not isinstance(value, dict):
            if value is not None:
                reasons.append(f"{label} is not an object")
            loaded_documents[label] = None
            continue
        loaded_documents[label] = value
        if value.get("schema_version") != SCHEMA_VERSION:
            reasons.append(f"{label} schema differs")
        if value.get("stage") != context.stage_name:
            reasons.append(f"{label} stage differs")
        if value.get("config_fingerprint") != config_fingerprint:
            reasons.append(f"{label} config fingerprint changed")
        if value.get("input_fingerprint") != inputs_fingerprint:
            reasons.append(f"{label} input fingerprint changed")
        document_outputs = value.get("outputs")
        if not isinstance(document_outputs, dict) or set(document_outputs) != required_names:
            reasons.append(f"{label} output allowlist differs")
        if label == "stage result" and value.get("status") != StageStatus.COMPLETE.value:
            reasons.append("stage result is not complete")
        if label == "stage result" and value.get("contract", {}).get("status") != "passed":
            reasons.append("stage result contract is not passed")

    for name, path in required_outputs.items():
        try:
            owned = assert_owned_path(path, stage_dir)
            actual = file_record(owned)
        except (FileNotFoundError, IsADirectoryError, OSError, ValueError) as error:
            reasons.append(f"required output {name!r} invalid: {error}")
            continue
        expected = recorded_outputs.get(name)
        if not isinstance(expected, dict) or expected.get("sha256") != actual["sha256"]:
            reasons.append(f"required output {name!r} hash mismatch")
        elif expected != actual:
            reasons.append(f"required output {name!r} record mismatch")
        for label, value in loaded_documents.items():
            document_outputs = value.get("outputs", {}) if value else {}
            expected = document_outputs.get(name)
            if not isinstance(expected, dict) or expected.get("sha256") != actual["sha256"]:
                reasons.append(f"{label} output {name!r} hash mismatch")
            elif expected != actual:
                reasons.append(f"{label} output {name!r} record mismatch")
    return ResumeDecision(not reasons, tuple(reasons))


def validate_resume_overwrite(*, resume: bool, overwrite: bool) -> None:
    if resume and overwrite:
        raise ValueError("resume and overwrite are mutually exclusive")


def assert_run_fingerprints(manifest: Mapping[str, Any], *, config_fingerprint: str, inputs_fingerprint: str) -> None:
    if manifest["config"]["fingerprint"] != config_fingerprint:
        raise ValueError("run config fingerprint changed; use a new run ID")
    if manifest["inputs"]["fingerprint"] != inputs_fingerprint:
        raise ValueError("run input fingerprint changed; use a new run ID")


def backup_manifest(context: RunContext) -> Path:
    source = context.run_dir / MANIFEST_FILENAME
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = context.run_dir / f"manifest.backup.{timestamp}.json"
    shutil.copy2(source, destination)
    return destination


def invalidate_from_stage(
    context: RunContext,
    *,
    ordered_stages: list[str],
    from_stage: str,
) -> Path:
    manifest = verify_run_ownership(context)
    if from_stage not in ordered_stages:
        raise ValueError(f"unknown stage: {from_stage}")
    backup = backup_manifest(context)
    for stage in ordered_stages[ordered_stages.index(from_stage):]:
        record = manifest["stages"].get(stage)
        if record is None:
            continue
        expected = context.for_stage(stage).stage_dir
        expected_directory = expected.relative_to(context.run_dir).as_posix()
        missing_clean_directory = (
            record.get("status") == StageStatus.NOT_STARTED.value
            and record.get("directory") is None
            and not expected.exists()
        )
        if record.get("directory") != expected_directory and not missing_clean_directory:
            raise ValueError(f"manifest does not own stage directory: {stage}")
        if expected.exists():
            owned = assert_owned_path(expected, context.run_dir)
            shutil.rmtree(owned)
        manifest["stages"][stage] = {
            "status": StageStatus.NOT_STARTED.value,
            "directory": expected_directory,
        }
    manifest["status"] = StageStatus.NOT_STARTED.value
    _write_manifest(context, manifest)
    return backup
