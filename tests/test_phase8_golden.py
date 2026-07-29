from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess

import geopandas as gpd
import pytest

from roadnet_partition.io.manifests import file_record, load_manifest
from roadnet_partition.pipeline.runner import _external_inputs, resolve_pipeline_config
from roadnet_partition.pipeline.stages import canonical_partition_output_key
from roadnet_partition.pipeline.validation import _grouping_hash, validate_run
from roadnet_partition.zoning.contracts import validate_partition
from test_phase7_release import complete_run


ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "artifacts/golden/beijing-fifth-ring-v1"
LEGACY = ROOT / "IntermediateDataForReproduce"
CLASSES = {
    "production-input", "golden-input", "golden-expected", "legacy-comparison",
    "release-candidate", "archive-only", "unknown",
}


def _manifest() -> dict:
    return json.loads((GOLDEN / "manifest.json").read_text(encoding="utf-8"))


def test_legacy_inventory_is_complete_unique_and_checksum_verified() -> None:
    if not LEGACY.is_dir() or len(list(LEGACY.iterdir())) <= 1:
        pytest.skip("local-only legacy payload is unavailable")
    manifest = _manifest()
    assets = manifest["assets"]
    old_files = {path.name for path in LEGACY.iterdir() if path.is_file()}
    assert len(assets) == len(old_files) == manifest["source_inventory"]["file_count"]
    assert sum((LEGACY / name).stat().st_size for name in old_files) == manifest["source_inventory"]["total_size"]
    assert {asset["source_old_path"] for asset in assets} == old_files
    assert len({asset["logical_name"] for asset in assets}) == len(assets)
    assert all(asset["classification"] in CLASSES for asset in assets)
    targets = [asset.get("relative_path") or asset.get("external_reference") for asset in assets]
    assert len(targets) == len(set(targets))
    assert all(file_record(LEGACY / asset["source_old_path"])["sha256"] == asset["sha256"] for asset in assets)
    for asset in assets:
        target = (GOLDEN / asset["relative_path"]) if "relative_path" in asset else (ROOT / asset["external_reference"])
        assert file_record(target)["sha256"] == asset["sha256"]
    privacy = Counter(asset["privacy"] for asset in assets)
    assert {name: privacy[name] for name in manifest["privacy_summary"]} == manifest["privacy_summary"]


def test_official_golden_partition_contract_and_read_only_payload() -> None:
    manifest = _manifest()
    contract = manifest["expected_contracts"]["partition"]
    asset = next(item for item in manifest["assets"] if item["logical_name"] == contract["asset"])
    path = GOLDEN / asset["relative_path"]
    if not path.is_file():
        pytest.skip("local-only Golden payload is unavailable")
    before = file_record(path)
    clusters = gpd.read_file(path)
    summary = validate_partition(clusters, expected_crs=contract["crs"], expected_bounds=contract["bounds"])
    assert summary["segment_count"] == contract["segment_count"] == 59096
    assert summary["cluster_count"] == contract["cluster_count"] == 100
    assert _grouping_hash(clusters) == contract["grouping_sha256"] == "11ac2e21b2f6f22498c250ee7eeaefe0f2c65ef5e5952e1c6722bac9154633c7"
    assert path.stat().st_mode & 0o222 == 0
    assert file_record(path) == before


def _tiny_golden(run_dir: Path, destination: Path) -> Path:
    run_manifest = load_manifest(run_dir)
    outputs = run_manifest["stages"]["partition"]["outputs"]
    source = Path(outputs[canonical_partition_output_key(outputs)]["path"])
    target = destination / "expected/partition/canonical.gpkg"
    target.parent.mkdir(parents=True)
    shutil.copy2(source, target)
    clusters = gpd.read_file(target)
    summary = validate_partition(clusters)
    record = file_record(target)
    value = {
        "schema_version": 1,
        "golden_id": "tiny-v1",
        "scope": "tiny",
        "created_at": "2026-01-01T00:00:00Z",
        "source_inventory": {"path": "synthetic", "file_count": 1, "total_size": record["size"]},
        "source_git_context": {"commit": None, "historical_runtime": "synthetic"},
        "assets": [{
            "logical_name": "canonical_partition_gpkg", "classification": "golden-expected",
            "relative_path": "expected/partition/canonical.gpkg", "size": record["size"],
            "sha256": record["sha256"], "format": "GeoPackage", "schema": "partition",
            "source_old_path": "synthetic", "reader": "validate", "writer": "tiny fixture",
            "storage": "local-only", "privacy": "synthetic",
        }],
        "expected_contracts": {"partition": {
            "asset": "canonical_partition_gpkg", "segment_count": summary["segment_count"],
            "cluster_count": summary["cluster_count"], "crs": summary["crs"],
            "grouping_sha256": _grouping_hash(clusters), "bounds": list(summary["bounds"]),
            "geometry": {"missing": 0, "empty": 0, "invalid": 0,
                         "types": clusters.geometry.geom_type.value_counts().sort_index().to_dict()},
        }},
        "known_provenance": ["synthetic fixture"], "unknown_provenance": [],
        "privacy_summary": {"synthetic": 1},
    }
    (destination / "manifest.json").write_text(json.dumps(value), encoding="utf-8")
    (destination / "checksums.sha256").write_text(
        f"{record['sha256']}  expected/partition/canonical.gpkg\n", encoding="utf-8",
    )
    return destination


def test_tiny_run_validates_against_golden_v1_without_modifying_golden(tmp_path: Path) -> None:
    _, run_dir = complete_run(tmp_path)
    golden = _tiny_golden(run_dir, tmp_path / "golden/tiny-v1")
    before = {path.relative_to(golden): path.read_bytes() for path in golden.rglob("*") if path.is_file()}
    report = validate_run(run_dir, golden=golden, write_report=False)
    after = {path.relative_to(golden): path.read_bytes() for path in golden.rglob("*") if path.is_file()}
    assert report["overall_status"] == "passed"
    assert report["golden_results"]["golden_id"] == "tiny-v1"
    assert before == after

    missing = tmp_path / "golden/missing-v1"
    shutil.copytree(golden, missing)
    (missing / "expected/partition/canonical.gpkg").unlink()
    assert validate_run(run_dir, golden=missing, write_report=False)["overall_status"] == "failed"

    damaged = tmp_path / "golden/damaged-v1"
    shutil.copytree(golden, damaged)
    payload = damaged / "expected/partition/canonical.gpkg"
    payload.write_bytes(payload.read_bytes() + b"damage")
    assert validate_run(run_dir, golden=damaged, write_report=False)["overall_status"] == "failed"


def test_production_full_config_resolves_and_external_inventory_exists() -> None:
    first = resolve_pipeline_config(ROOT / "configs/pipelines/full.yaml")
    second = resolve_pipeline_config(ROOT / "configs/pipelines/full.yaml")
    assert first.fingerprint == second.fingerprint
    assert first.scope == "fifth_ring"
    if not (ROOT / "data/interim/fifth_ring/frozen_inputs/segment_relation_graph_road_poi_order.gpickle").is_file():
        pytest.skip("local-only production frozen input is unavailable")
    inventory = _external_inputs(first)
    assert inventory
    assert all(Path(record["path"]).is_file() for record in inventory.values())
    assert not any("IntermediateDataForReproduce" in str(record["path"]) for record in inventory.values())
    assert Path(first.stages["demand"].values["order_pipeline"]["inputs"]["partition_gpkg"]) == (
        ROOT / "data/processed/fifth_ring/partition/canonical_partition.gpkg"
    )


def test_golden_version_is_immutable_by_policy() -> None:
    readme = (GOLDEN / "README.md").read_text(encoding="utf-8")
    assert "new version directory" in readme
    assert "reproduction release" in readme


def test_phase8_config_docs_package_and_git_boundaries() -> None:
    formal_configs = [
        ROOT / "configs/datasets/fifth_ring.yaml", ROOT / "configs/datasets/fourth_ring.yaml",
        ROOT / "configs/zoning/regularized.yaml", ROOT / "configs/pipelines/demand.yaml",
        ROOT / "configs/pipelines/supply.yaml", ROOT / "configs/pipelines/tte.yaml",
        ROOT / "configs/pipelines/full.yaml",
    ]
    legacy_name = _manifest()["source_inventory"]["path"]
    assert all(legacy_name not in path.read_text(encoding="utf-8") for path in formal_configs)
    assert all(
        legacy_name not in path.read_text(encoding="utf-8")
        for path in (ROOT / "src/roadnet_partition").rglob("*.py")
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "three-stage" not in readme and "三阶段" not in readme
    for command in ("roadnet-partition run", "roadnet-partition validate", "roadnet-partition publish", "export-reproduction"):
        assert command in readme
    assert "Phase 9" in readme and "--dry-run" in readme

    migration = (ROOT / "docs/refactor/production-config-path-migration-v2.md").read_text(encoding="utf-8")
    assert "36 authoritative path comparisons" in migration
    assert "Migrated paths (15)" in migration and "Unchanged path comparisons (21)" in migration
    assert "151 authoritative effective non-path values" in migration

    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(GOLDEN / "expected/partition/segment_clusters_road_poi_order_regularized_leiden_lc1p0_lr1p0.gpkg")],
        cwd=ROOT, check=False,
    )
    assert ignored.returncode == 0
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(GOLDEN / "expected/partition/segment_clusters_road_poi_order_regularized_leiden_lc1p0_lr1p0.gpkg")],
        cwd=ROOT, check=False, capture_output=True,
    )
    assert tracked.returncode != 0
