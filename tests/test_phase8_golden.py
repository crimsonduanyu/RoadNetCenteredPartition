from __future__ import annotations

import json
from pathlib import Path
import shutil

import geopandas as gpd

from roadnet_partition.io.manifests import file_record, load_manifest
from roadnet_partition.pipeline.runner import _external_inputs, resolve_pipeline_config
from roadnet_partition.pipeline.stages import canonical_partition_output_key
from roadnet_partition.pipeline.validation import _grouping_hash, validate_run
from roadnet_partition.zoning.contracts import validate_partition
from test_phase7_release import complete_run


ROOT = Path(__file__).resolve().parents[1]
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
        "source_inventory": {"path": "synthetic", "file_count": 2, "total_size": record["size"]},
        "source_git_context": {"commit": None, "historical_runtime": "synthetic"},
        "assets": [{
            "logical_name": "canonical_partition_gpkg", "classification": "golden-expected",
            "relative_path": "expected/partition/canonical.gpkg", "size": record["size"],
            "sha256": record["sha256"], "format": "GeoPackage", "schema": "partition",
            "source_old_path": "synthetic", "reader": "validate", "writer": "tiny fixture",
            "storage": "local-only", "privacy": "synthetic",
        }, {
            "logical_name": "retired_readme", "classification": "archive-only",
            "external_reference": "retired/README.md", "size": 0,
            "sha256": "0" * 64, "format": "markdown", "schema": "none",
            "source_old_path": "README.md", "reader": "human", "writer": "tiny fixture",
            "storage": "external", "privacy": "public",
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
    inventory = _external_inputs(first)
    assert set(inventory) == {
        "preparation.raw_edges", "preparation.boundary", "preparation.ring_segments",
        "preparation.poi", "preparation.zoning_orders", "demand.orders.0",
        "demand.poi", "tte.graphml",
    }
    assert all(Path(record["path"]).is_file() for record in inventory.values())


def test_phase8_config_docs_package_and_git_boundaries() -> None:
    formal_configs = [
        ROOT / "configs/datasets/fifth_ring.yaml", ROOT / "configs/datasets/fourth_ring.yaml",
        ROOT / "configs/zoning/regularized.yaml", ROOT / "configs/pipelines/demand.yaml",
        ROOT / "configs/pipelines/supply.yaml", ROOT / "configs/pipelines/tte.yaml",
        ROOT / "configs/pipelines/full.yaml",
    ]
    assert all("artifacts/" not in path.read_text(encoding="utf-8") for path in formal_configs)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "three-stage" not in readme and "三阶段" not in readme
    for command in ("roadnet-partition run", "roadnet-partition validate", "roadnet-partition publish", "export-reproduction"):
        assert command in readme
    assert "data/raw/" in readme and "--resume" in readme

    history = (ROOT / "docs/history/refactor-v1.md").read_text(encoding="utf-8")
    assert "split configuration" in history
    assert "Linux is the current Fifth Ring canonical platform" in history
    assert "transactional publishing" in history
