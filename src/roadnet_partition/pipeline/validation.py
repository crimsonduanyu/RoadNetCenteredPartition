from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Mapping

import geopandas as gpd
import yaml

from roadnet_partition import __version__
from roadnet_partition.config import ResolvedStageConfig, config_fingerprint, stable_value
from roadnet_partition.io.manifests import (
    RUN_MARKER,
    atomic_write_json,
    atomic_write_text,
    evaluate_resume,
    file_record,
    input_fingerprint,
    load_manifest,
    utc_now,
    verify_run_ownership,
)
from roadnet_partition.io.paths import assert_owned_path
from roadnet_partition.pipeline.results import RunContext, StageStatus
from roadnet_partition.pipeline.stages import (
    STAGE_ORDER,
    canonical_partition_output_key,
    formal_stage_outputs,
    prepare_stage_contract_config,
    validate_stage_contract,
)
from roadnet_partition.zoning.contracts import compare_partitions, partition_groups, validate_partition


class ValidationError(RuntimeError):
    pass


_EXPECTED_BINDINGS = {
    "demand": {"order_pipeline.inputs.partition_gpkg"},
    "supply": {"stage3_supply.orders_path", "stage3_supply.cluster_index_path"},
    "tte": {"stage4_tte.inputs.orders_path", "stage4_tte.inputs.cluster_index_path"},
}


def _read_yaml(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"resolved snapshot may not be a symbolic link: {path}")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"resolved snapshot is not a mapping: {path}")
    return value


def _load_stage_config(path: Path, stage: str, project_root: Path) -> tuple[ResolvedStageConfig, dict[str, Any]]:
    snapshot = _read_yaml(path)
    if snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("resolved"), dict):
        raise ValueError(f"invalid resolved {stage} snapshot")
    values = snapshot["resolved"]
    fingerprint = config_fingerprint(values)
    if fingerprint != snapshot.get("fingerprint"):
        raise ValueError(f"resolved {stage} snapshot fingerprint differs")
    config = ResolvedStageConfig(
        Path(snapshot["source_path"]), values, fingerprint, stage,
        str(values["_resolved"]["scope"]), project_root,
        None if snapshot.get("dataset_path") is None else Path(snapshot["dataset_path"]),
    )
    return config, snapshot


def _field(values: Mapping[str, Any], dotted: str) -> Any:
    current: Any = values
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return stable_value(current)


def _grouping_hash(clusters: gpd.GeoDataFrame) -> str:
    digest = hashlib.sha256()
    groups = sorted((sorted(group) for group in partition_groups(clusters)), key=lambda members: (len(members), members))
    for members in groups:
        digest.update("\t".join(members).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _asset_path(root: Path, project_root: Path, asset: Mapping[str, Any]) -> Path:
    if "relative_path" in asset:
        owner, value = root, asset["relative_path"]
    elif "external_reference" in asset:
        owner, value = project_root, asset["external_reference"]
    else:
        raise ValueError(f"Golden asset {asset.get('logical_name')!r} has no location")
    raw = owner / str(value)
    for current in (raw, *raw.parents):
        if current.exists() and current.is_symlink():
            raise ValueError(f"Golden asset path contains a symbolic link: {current}")
        if current == owner:
            break
    resolved = raw.resolve()
    if not resolved.is_relative_to(owner.resolve()):
        raise ValueError(f"Golden asset escapes its owner: {value}")
    return resolved


def _golden_v1(
    root: Path,
    value: Mapping[str, Any],
    manifest: Mapping[str, Any],
    project_root: Path,
    run_dir: Path,
) -> dict[str, Any]:
    required = {
        "schema_version", "golden_id", "scope", "created_at", "source_inventory",
        "source_git_context", "assets", "expected_contracts", "known_provenance",
        "unknown_provenance", "privacy_summary",
    }
    if set(value) != required or value.get("schema_version") != 1:
        raise ValueError("Golden v1 manifest schema is invalid")
    if value["scope"] != manifest.get("scope"):
        raise ValueError("Golden scope differs")
    assets = value["assets"]
    if not isinstance(assets, list) or not assets:
        raise ValueError("Golden assets must be a non-empty list")
    by_name: dict[str, tuple[Mapping[str, Any], Path]] = {}
    local_checksums: dict[str, str] = {}
    allowed_classes = {
        "production-input", "golden-input", "golden-expected", "legacy-comparison",
        "release-candidate", "archive-only", "unknown",
    }
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("Golden asset entry is invalid")
        name = asset.get("logical_name")
        if not isinstance(name, str) or name in by_name:
            raise ValueError(f"Golden logical asset name is invalid or duplicated: {name!r}")
        if asset.get("classification") not in allowed_classes:
            raise ValueError(f"Golden asset classification is invalid: {name}")
        path = _asset_path(root, project_root, asset)
        if not path.is_file():
            raise ValueError(f"Golden asset is missing: {path}")
        record = file_record(path)
        if record["size"] != asset.get("size") or record["sha256"] != asset.get("sha256"):
            raise ValueError(f"Golden asset checksum differs: {name}")
        by_name[name] = asset, path
        if "relative_path" in asset:
            local_checksums[str(asset["relative_path"])] = str(asset["sha256"])

    checksum_path = root / "checksums.sha256"
    declared = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        if relative in declared:
            raise ValueError(f"Golden checksum path is duplicated: {relative}")
        declared[relative] = digest
    if declared != local_checksums:
        raise ValueError("Golden checksums.sha256 differs from local asset inventory")

    contract = value["expected_contracts"].get("partition")
    if not isinstance(contract, dict):
        raise ValueError("Golden Partition expected contract is missing")
    expected_asset, expected_path = by_name[contract["asset"]]
    if expected_asset.get("classification") != "golden-expected":
        raise ValueError("Golden Partition contract does not reference golden-expected")
    expected = gpd.read_file(expected_path)
    expected_summary = validate_partition(
        expected,
        expected_crs=contract["crs"],
        expected_bounds=contract["bounds"],
    )
    if expected_summary["segment_count"] != contract["segment_count"]:
        raise ValueError("Golden Partition segment count differs")
    if expected_summary["cluster_count"] != contract["cluster_count"]:
        raise ValueError("Golden Partition cluster count differs")
    grouping_hash = _grouping_hash(expected)
    if grouping_hash != contract["grouping_sha256"]:
        raise ValueError("Golden Partition grouping hash differs")
    geometry_types = expected.geometry.geom_type.value_counts().sort_index().to_dict()
    if geometry_types != contract["geometry"]["types"]:
        raise ValueError("Golden Partition geometry types differ")

    partition_outputs = manifest["stages"]["partition"]["outputs"]
    actual_path = Path(partition_outputs[canonical_partition_output_key(partition_outputs)]["path"])
    actual = gpd.read_file(actual_path)
    actual_summary = validate_partition(
        actual,
        expected_segment_ids=expected["seg_id"].astype(str),
        expected_crs=expected.crs,
        expected_bounds=expected.total_bounds,
    )
    if not compare_partitions(actual, expected):
        raise ValueError("run Partition grouping differs from Golden expected")

    baseline = contract.get("baseline_initialization")
    if baseline:
        baseline_asset, baseline_path = by_name[baseline["asset"]]
        if baseline_asset.get("classification") != "golden-input":
            raise ValueError("Golden baseline initialization is not a golden-input")
        config, _ = _load_stage_config(
            run_dir / "resolved_configs/partition.yaml", "partition", project_root,
        )
        configured = Path(config.values["inputs"]["baseline_clusters"][baseline["name"]])
        if file_record(configured)["sha256"] != file_record(baseline_path)["sha256"]:
            raise ValueError("run baseline initialization differs from Golden")

    return {
        "status": "passed",
        "golden_id": value["golden_id"],
        "manifest": (root / "manifest.json").as_posix(),
        "asset_count": len(assets),
        "partition": {
            "segment_count": actual_summary["segment_count"],
            "cluster_count": actual_summary["cluster_count"],
            "crs": actual_summary["crs"],
            "grouping_sha256": grouping_hash,
        },
    }


def _golden_result(
    golden: Path | None,
    manifest: Mapping[str, Any],
    project_root: Path,
    run_dir: Path,
) -> dict[str, Any] | None:
    if golden is None:
        return None
    raw_root = golden.expanduser().absolute()
    if any(current.exists() and current.is_symlink() for current in (raw_root, *raw_root.parents)):
        raise ValueError("Golden path may not contain a symbolic link")
    root = raw_root.resolve()
    source = root / "manifest.json" if root.is_dir() else root
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Golden manifest schema is invalid")
    if "golden_id" in value:
        return _golden_v1(
            root if root.is_dir() else source.parent, value, manifest, project_root, run_dir,
        )
    checksums = value.get("checksums")
    expected_contract = value.get("expected_contract")
    if not isinstance(checksums, list) or not isinstance(expected_contract, dict):
        raise ValueError("Golden manifest must declare checksums and expected_contract")
    compared = []
    for expected in checksums:
        if not isinstance(expected, dict) or set(expected) != {"stage", "logical_key", "sha256"}:
            raise ValueError("Golden checksum entry is invalid")
        record = manifest["stages"].get(expected["stage"], {}).get("outputs", {}).get(expected["logical_key"])
        if not isinstance(record, dict) or record.get("sha256") != expected["sha256"]:
            raise ValueError(
                f"Golden checksum differs: {expected['stage']}.{expected['logical_key']}"
            )
        compared.append(f"{expected['stage']}.{expected['logical_key']}")
    if "scope" in expected_contract and expected_contract["scope"] != manifest["scope"]:
        raise ValueError("Golden expected scope differs")
    return {"status": "passed", "manifest": source.as_posix(), "compared": compared}


def _report_paths(run_dir: Path, report: Path | None) -> tuple[Path, Path]:
    if report is None:
        directory = assert_owned_path(run_dir / "validation", run_dir)
        return directory / "validation_report.json", directory / "validation_report.md"
    destination = assert_owned_path(report, run_dir)
    if destination.suffix:
        return destination, destination.with_suffix(".md")
    return destination / "validation_report.json", destination / "validation_report.md"


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# RoadNet pipeline validation",
        "",
        f"- Run: `{report.get('run_id')}`",
        f"- Status: **{report.get('overall_status')}**",
        f"- Validated at: `{report.get('validated_at')}`",
        "",
        "## Stages",
        "",
    ]
    for stage, result in report.get("stage_results", {}).items():
        lines.append(f"- `{stage}`: {result.get('status')}")
    lines.extend(["", "## Warnings", ""])
    lines.extend(f"- {warning}" for warning in report.get("warnings", []))
    lines.extend(["", "## Errors", ""])
    lines.extend(f"- {error}" for error in report.get("errors", []))
    return "\n".join(lines) + "\n"


def validate_run(
    run: str | Path,
    *,
    golden: str | Path | None = None,
    report: str | Path | None = None,
    write_report: bool = True,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    run_dir = Path(run).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    stage_results: dict[str, Any] = {}
    hash_results: dict[str, Any] = {}
    contract_results: dict[str, Any] = {}
    binding_results: dict[str, Any] = {}
    golden_results = None
    manifest: dict[str, Any] = {}
    run_id = run_dir.name
    context: RunContext | None = None
    try:
        if run_dir.is_symlink():
            raise ValueError("run directory may not be a symbolic link")
        marker_path = run_dir / RUN_MARKER
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        run_id = str(marker["run_id"])
        manifest = load_manifest(run_dir)
        project_root = Path(manifest["config"]["resolved"]["project_root"]).resolve()
        context = RunContext(run_id, run_dir, project_root, log_dir=run_dir / "logs")
        verify_run_ownership(context)
        if manifest.get("run_kind") != "pipeline":
            raise ValueError("run is not a pipeline run")
        pipeline = manifest.get("pipeline")
        if not isinstance(pipeline, dict):
            raise ValueError("pipeline manifest section is missing")
        if pipeline.get("stage_order") != list(STAGE_ORDER):
            errors.append("pipeline stage order differs")
        if not isinstance(pipeline.get("invocation_history"), list) or not pipeline["invocation_history"]:
            errors.append("pipeline invocation history is missing")
        if pipeline.get("completed_through") != "tte":
            errors.append("pipeline completed_through is not tte")
        if pipeline.get("all_required_stages_complete") is not True:
            errors.append("pipeline all_required_stages_complete is not true")
        if pipeline.get("requested_from") not in STAGE_ORDER or pipeline.get("requested_to") not in STAGE_ORDER:
            errors.append("pipeline requested range is invalid")

        pipeline_snapshot = _read_yaml(run_dir / "resolved_config.yaml")
        if pipeline_snapshot.get("fingerprint") != pipeline.get("config_fingerprint"):
            errors.append("pipeline resolved snapshot fingerprint differs")
        if config_fingerprint(pipeline_snapshot.get("resolved", {})) != pipeline.get("config_fingerprint"):
            errors.append("pipeline resolved values fingerprint differs")
        required = {
            stage: bool(manifest["config"]["resolved"]["stages"][stage]["required"])
            for stage in STAGE_ORDER
        }
        scopes = set()
        snapshots: dict[str, tuple[ResolvedStageConfig, dict[str, Any]]] = {}
        for stage in STAGE_ORDER:
            if not required[stage]:
                continue
            try:
                config, snapshot = _load_stage_config(
                    run_dir / "resolved_configs" / f"{stage}.yaml", stage, context.project_root,
                )
                snapshots[stage] = config, snapshot
                scopes.add(config.scope)
                record = manifest["stages"].get(stage, {})
                if record.get("status") != StageStatus.COMPLETE.value:
                    raise ValueError(f"stage status is {record.get('status')!r}")
                if record.get("config_fingerprint") != config.fingerprint:
                    raise ValueError("stage config fingerprint differs")
                outputs = formal_stage_outputs(stage, config, context.for_stage(stage).stage_dir)
                decision = evaluate_resume(
                    context.for_stage(stage),
                    config_fingerprint=config.fingerprint,
                    inputs_fingerprint=input_fingerprint(record.get("input_records", {})),
                    required_outputs=outputs,
                    require_run_complete=False,
                )
                if not decision.reusable:
                    raise ValueError("; ".join(decision.reasons))
                contract = validate_stage_contract(
                    stage,
                    prepare_stage_contract_config(stage, config, record.get("input_records", {})),
                    outputs,
                )
                if contract.get("status") != "passed":
                    raise ValueError("stage contract did not pass")
                stage_results[stage] = {"status": "passed", "config_fingerprint": config.fingerprint}
                hash_results[stage] = {name: value["sha256"] for name, value in record["outputs"].items()}
                contract_results[stage] = contract
            except Exception as error:
                stage_results[stage] = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
                errors.append(f"{stage}: {error}")
        if scopes and scopes != {manifest.get("scope")}:
            errors.append(f"stage scopes differ from run scope: {sorted(scopes)}")

        for stage, (config, snapshot) in snapshots.items():
            try:
                bindings = snapshot.get("runtime_bindings")
                record_bindings = manifest["stages"][stage].get("runtime_bindings")
                if not isinstance(bindings, list) or bindings != record_bindings:
                    raise ValueError("snapshot/manifest runtime bindings differ")
                expected = set(_EXPECTED_BINDINGS.get(stage, set()))
                if stage == "tte" and not config.values["stage4_tte"]["inputs"].get("network_distance_path"):
                    expected.add("stage4_tte.distance.partition_gpkg")
                if {item.get("consumer_config_field") for item in bindings} != expected:
                    raise ValueError("runtime binding allowlist differs")
                for binding in bindings:
                    producer = binding.get("producer_stage")
                    key = binding.get("producer_logical_key")
                    if binding.get("producer_run_id") != run_id or binding.get("consumer_stage") != stage:
                        raise ValueError("binding crosses run/stage boundary")
                    producer_record = manifest["stages"].get(producer, {})
                    output = producer_record.get("outputs", {}).get(key)
                    if producer_record.get("status") != "complete" or not isinstance(output, dict):
                        raise ValueError("binding producer output is incomplete")
                    if any(binding.get(name) != output.get(name) for name in ("path", "size", "sha256")):
                        raise ValueError("binding producer hash/path differs")
                    if _field(config.values, binding["consumer_config_field"]) != binding["path"]:
                        raise ValueError("standalone fallback was used instead of pipeline binding")
                    input_record = manifest["stages"][stage].get("input_records", {}).get(binding["consumer_input_key"])
                    if not isinstance(input_record, dict) or input_record.get("sha256") != binding["sha256"]:
                        raise ValueError("binding input fingerprint provenance differs")
                    if binding.get("pipeline_binding_wins") is not True:
                        raise ValueError("binding precedence is not recorded")
                binding_results[stage] = {"status": "passed", "count": len(bindings)}
            except Exception as error:
                binding_results[stage] = {"status": "failed", "error": str(error)}
                errors.append(f"{stage} bindings: {error}")

        try:
            golden_results = _golden_result(
                None if golden is None else Path(golden), manifest, context.project_root, run_dir,
            )
        except Exception as error:
            golden_results = {"status": "failed", "error": str(error)}
            errors.append(f"Golden: {error}")
        if manifest.get("git", {}).get("dirty") is True:
            warnings.append("source run was created from a dirty Git worktree")
    except Exception as error:
        errors.append(f"run: {type(error).__name__}: {error}")

    report_value = {
        "schema_version": 1,
        "run_id": run_id,
        "validated_at": utc_now(),
        "validator_version": __version__,
        "git_runtime_summary": {
            "git": manifest.get("git"),
            "runtime": manifest.get("runtime"),
        },
        "stage_results": stage_results,
        "hash_results": hash_results,
        "contract_results": contract_results,
        "binding_results": binding_results,
        "golden_results": golden_results,
        "warnings": warnings,
        "errors": errors,
        "overall_status": "passed" if not errors else "failed",
    }
    if write_report and context is not None:
        json_path, markdown_path = _report_paths(run_dir, None if report is None else Path(report))
        atomic_write_json(json_path, report_value)
        atomic_write_text(markdown_path, _markdown(report_value))
    if errors and raise_on_error:
        raise ValidationError("validation failed: " + "; ".join(errors))
    return report_value


__all__ = ["ValidationError", "validate_run"]
