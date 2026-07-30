from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping
import uuid

import yaml

from roadnet_partition.io.manifests import (
    MANIFEST_FILENAME,
    atomic_write_json,
    collect_git_info,
    file_record,
    load_manifest,
    utc_now,
    validate_manifest,
)
from roadnet_partition.io.paths import transactional_scope_swap
from roadnet_partition.pipeline.results import RunContext
from roadnet_partition.pipeline.stages import STAGE_ORDER, canonical_partition_output_key, validate_stage_contract
from roadnet_partition.pipeline.validation import _load_stage_config, validate_run


class PublishError(RuntimeError):
    pass


_DEMAND_KEYS = (
    "cluster_index", "orders_region_assigned", "cluster_od", "od_tensor", "metadata",
    "road_graph_edges", "road_adjacency_raw", "road_adjacency_normalized",
    "poi_graph_edges", "poi_adjacency_raw", "poi_adjacency_normalized",
    "distance_graph_edges", "distance_adjacency_raw", "distance_adjacency_normalized",
    "poi_features", "poi_category_mapping",
)
_SUPPLY_NAMES = {
    "inservice_od": "supply_inservice_od.csv.gz",
    "available_floor": "supply_available_floor.csv.gz",
    "fleet_lower_bound": "supply_fleet_lower_bound.csv.gz",
    "run_summary": "run_summary.json",
    "config_used": "config_used.json",
}
_TTE_NAMES = {
    "network_distance": "cluster_network_distance.parquet",
    "representative_nodes": "cluster_representative_nodes.csv",
    "tte_raw": "TTE_raw.parquet",
    "tte_count": "TTE_count.parquet",
    "tte_support": "TTE_support.parquet",
    "tte_hops": "TTE_hops.parquet",
    "tte_imputed": "TTE_imputed.parquet",
}


def _context(run_dir: Path, manifest: Mapping[str, Any]) -> RunContext:
    project_root = Path(manifest["config"]["resolved"]["project_root"]).resolve()
    return RunContext(str(manifest["run_id"]), run_dir, project_root, log_dir=run_dir / "logs")


def _dirty_git(manifest: Mapping[str, Any], project_root: Path, allow_dirty: bool) -> dict[str, Any]:
    current = collect_git_info(project_root)
    source_dirty = manifest.get("git", {}).get("dirty") is True
    current_dirty = current.get("dirty") is True
    if (source_dirty or current_dirty) and not allow_dirty:
        raise PublishError("dirty Git state requires --allow-dirty")
    return {"source": manifest.get("git"), "current": current, "allowed": bool(allow_dirty)}


def build_publish_inventory(run: str | Path) -> list[dict[str, Any]]:
    run_dir = Path(run).resolve()
    manifest = load_manifest(run_dir)
    records: list[dict[str, Any]] = []

    partition_outputs = manifest["stages"]["partition"]["outputs"]
    gpkg_key = canonical_partition_output_key(partition_outputs)
    csv_key = gpkg_key.replace("cluster_gpkg_", "cluster_csv_", 1)
    for key, relative in (
        (gpkg_key, "partition/canonical_partition.gpkg"),
        (csv_key, "partition/canonical_partition.csv"),
    ):
        records.append(_inventory_record("partition", key, partition_outputs[key], relative))

    demand_outputs = manifest["stages"]["demand"]["outputs"]
    for key in _DEMAND_KEYS:
        source = Path(demand_outputs[key]["path"])
        records.append(_inventory_record("demand", key, demand_outputs[key], f"order_pipeline/{source.name}"))
    for key, filename in _SUPPLY_NAMES.items():
        records.append(_inventory_record("supply", key, manifest["stages"]["supply"]["outputs"][key], f"supply/{filename}"))
    for key, filename in _TTE_NAMES.items():
        records.append(_inventory_record("tte", key, manifest["stages"]["tte"]["outputs"][key], f"tte/{filename}"))
    return records


def _inventory_record(stage: str, key: str, record: Mapping[str, Any], relative: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "logical_key": key,
        "run_source_path": record["path"],
        "formal_relative_path": relative,
        "size": int(record["size"]),
        "sha256": str(record["sha256"]),
        "schema_contract_version": 1,
    }


def _source_manifest(
    run_dir: Path,
    manifest: Mapping[str, Any],
    inventory: list[dict[str, Any]],
    validation: Mapping[str, Any],
    git: Mapping[str, Any],
    transaction: Mapping[str, Any],
    baseline_decision: Mapping[str, Any] | None,
) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "scope": manifest["scope"],
        "published_at": utc_now(),
        "source_run_id": manifest["run_id"],
        "source_run_path": run_dir.as_posix(),
        "source_git": manifest["git"],
        "publish_git": git,
        "source_runtime": manifest["runtime"],
        "pipeline_config_fingerprint": manifest["pipeline"]["config_fingerprint"],
        "stage_config_fingerprints": {
            stage: manifest["stages"][stage]["config_fingerprint"] for stage in STAGE_ORDER
        },
        "bindings": {
            stage: manifest["stages"][stage].get("runtime_bindings", []) for stage in STAGE_ORDER
        },
        "published_files": inventory,
        "validation_summary": {
            "overall_status": validation["overall_status"],
            "validator_version": validation["validator_version"],
            "validated_at": validation["validated_at"],
        },
        "publish_transaction": transaction,
    }
    if baseline_decision is not None:
        value["baseline_decision"] = dict(baseline_decision)
    return value


def _baseline_decision(
    path: str | Path | None,
    *,
    run_dir: Path,
    manifest: Mapping[str, Any],
    scope: str,
) -> dict[str, Any] | None:
    if path is None:
        if scope == "fifth_ring":
            raise PublishError("fifth_ring publish requires --baseline-decision")
        return None
    source = Path(path).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise PublishError("baseline decision must be a regular file")
    value = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise PublishError("invalid baseline decision schema")
    source_run = value.get("source_run")
    previous = value.get("previous_canonical")
    if value.get("status") != "approved" or value.get("decision") != "adopt_linux_as_canonical":
        raise PublishError("baseline decision is not approved")
    if value.get("scope") != scope or not isinstance(source_run, dict) or not isinstance(previous, dict):
        raise PublishError("baseline decision scope/source is invalid")
    report_path = run_dir / "validation" / "validation_report.json"
    if not report_path.is_file():
        raise PublishError("baseline decision requires an existing validation report")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    expected = {
        "run_id": manifest["run_id"],
        "pipeline_config_fingerprint": manifest["pipeline"]["config_fingerprint"],
        "demand_assigned_orders_sha256": manifest["stages"]["demand"]["outputs"]["orders_region_assigned"]["sha256"],
        "validation_report_sha256": file_record(report_path)["sha256"],
    }
    if any(source_run.get(key) != expected_value for key, expected_value in expected.items()):
        raise PublishError("baseline decision does not match this run")
    if report.get("run_id") != manifest["run_id"] or report.get("overall_status") != "passed":
        raise PublishError("baseline decision validation report is not a passing report for this run")
    return {
        "decision_id": value.get("decision_id"),
        "path": source.as_posix(),
        "sha256": file_record(source)["sha256"],
        "previous_canonical_archive_id": previous.get("archive_id"),
    }


def _copy_inventory(staging: Path, inventory: list[dict[str, Any]]) -> None:
    for item in inventory:
        source = Path(item["run_source_path"])
        destination = staging / item["formal_relative_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _stage_output_paths(staging: Path, inventory: list[dict[str, Any]], stage: str) -> dict[str, Path]:
    return {
        item["logical_key"]: staging / item["formal_relative_path"]
        for item in inventory if item["stage"] == stage
    }


def _validate_staging(staging: Path, run_dir: Path, inventory: list[dict[str, Any]]) -> bool:
    expected = {item["formal_relative_path"] for item in inventory} | {"source_manifest.json"}
    actual = {
        path.relative_to(staging).as_posix()
        for path in staging.rglob("*") if path.is_file()
    }
    if actual != expected:
        raise PublishError(f"staging allowlist differs: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")
    source_manifest = json.loads((staging / "source_manifest.json").read_text(encoding="utf-8"))
    if source_manifest.get("schema_version") != 1:
        raise PublishError("source manifest schema differs")
    manifest = load_manifest(run_dir)
    context = _context(run_dir, manifest)
    for item in inventory:
        copied = file_record(staging / item["formal_relative_path"])
        if copied["size"] != item["size"] or copied["sha256"] != item["sha256"]:
            raise PublishError(f"published file hash differs: {item['formal_relative_path']}")
    for stage in STAGE_ORDER:
        config, _ = _load_stage_config(run_dir / "resolved_configs" / f"{stage}.yaml", stage, context.project_root)
        contract = validate_stage_contract(stage, config, _stage_output_paths(staging, inventory, stage))
        if contract.get("status") != "passed":
            raise PublishError(f"published {stage} contract did not pass")
    return True


def _disk_free(path: Path) -> int:
    current = path
    while not current.exists():
        current = current.parent
    return shutil.disk_usage(current).free


def publish_scope(
    run: str | Path,
    *,
    scope: str,
    overwrite: bool = False,
    allow_dirty: bool = False,
    dry_run: bool = False,
    baseline_decision: str | Path | None = None,
    _step_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    run_dir = Path(run).expanduser().resolve()
    manifest = load_manifest(run_dir)
    decision = _baseline_decision(
        baseline_decision, run_dir=run_dir, manifest=manifest, scope=scope,
    )
    validation = validate_run(run_dir, write_report=False)
    if validation["overall_status"] != "passed":
        raise PublishError("current run validation failed")
    if manifest.get("run_kind") != "pipeline" or manifest["pipeline"].get("all_required_stages_complete") is not True:
        raise PublishError("publish requires a complete pipeline run")
    if scope != manifest.get("scope"):
        raise PublishError(f"publish scope {scope!r} differs from run scope {manifest.get('scope')!r}")
    context = _context(run_dir, manifest)
    git = _dirty_git(manifest, context.project_root, allow_dirty)
    inventory = build_publish_inventory(run_dir)
    total_size = sum(item["size"] for item in inventory)
    target = context.project_root / "data" / "processed" / scope
    if any(path.is_symlink() for path in (target, *target.parents) if path.exists()):
        raise PublishError("publish target path contains a symbolic link")
    if target.exists() and not overwrite:
        raise FileExistsError(f"published scope already exists; use --overwrite: {target}")
    staging = target.parent / f".{scope}.staging-{manifest['run_id']}-{uuid.uuid4().hex[:8]}"
    transaction = {
        "mode": "overwrite" if target.exists() else "create",
        "target": target.as_posix(),
        "staging": staging.as_posix(),
        "overwrite": overwrite,
        "dry_run": dry_run,
    }
    result = {
        "schema_version": 1,
        "status": "dry_run" if dry_run else "published",
        "run_id": manifest["run_id"],
        "scope": scope,
        "target": target.as_posix(),
        "file_count": len(inventory),
        "total_size": total_size,
        "free_space": _disk_free(target.parent),
        "transaction": transaction,
        "git": git,
        "baseline_decision": decision,
    }
    if result["free_space"] < total_size:
        raise PublishError("insufficient disk space for publish staging")
    if dry_run:
        atomic_write_json(run_dir / "validation" / "publish_dry_run.json", result)
        return result

    staging.mkdir(parents=True)
    _copy_inventory(staging, inventory)
    source_manifest = _source_manifest(run_dir, manifest, inventory, validation, git, transaction, decision)
    atomic_write_json(staging / "source_manifest.json", source_manifest)

    def validate_and_hook(path: Path) -> bool:
        return _validate_staging(path, run_dir, inventory)

    def transaction_hook(step: str) -> None:
        if step == "staging_moved_to_target":
            _validate_staging(target, run_dir, inventory)
        if _step_hook:
            _step_hook(step)

    transactional_scope_swap(
        target, staging, validate=validate_and_hook, overwrite=overwrite, _step_hook=transaction_hook,
    )
    manifest = load_manifest(run_dir)
    manifest["publish_history"].append({
        "published_at": source_manifest["published_at"],
        "scope": scope,
        "target": target.as_posix(),
        "overwrite": overwrite,
        "source_manifest_sha256": file_record(target / "source_manifest.json")["sha256"],
        "git": git,
        "baseline_decision": decision,
    })
    atomic_write_json(run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    return result


__all__ = ["PublishError", "build_publish_inventory", "publish_scope"]
