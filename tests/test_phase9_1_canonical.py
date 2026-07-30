from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "artifacts/baselines/fifth-ring-windows-v1"


def test_linux_canonical_decision_is_approved_and_run_bound() -> None:
    value = yaml.safe_load((ROOT / "configs/policies/fifth_ring_linux_canonical_v1.yaml").read_text())
    assert value["status"] == "approved"
    assert value["decision"] == "adopt_linux_as_canonical"
    assert value["scope"] == "fifth_ring"
    assert value["source_run"]["run_id"] == "20260730T020500Z-fifth-ring-full-02fce2f7"
    assert value["previous_canonical"]["archive_id"] == "fifth-ring-windows-v1"
    assert value["consequences"]["deterministic_assignment_v2"] == "deferred"


def test_windows_archive_metadata_payload_and_ignore_rule() -> None:
    manifest = json.loads((ARCHIVE / "manifest.json").read_text())
    assert manifest["file_count"] == 91
    assert manifest["total_size"] == 7_588_535_926
    assert manifest["inventory_sha256"] == "0815dc61d0e8ac950229372a4b0f7fae76adae8c410a1535328c112c8fad9540"
    payload = ARCHIVE / "payload"
    if not payload.is_dir():
        pytest.skip("private local-only Windows archive payload is unavailable")
    first = payload / manifest["files"][0]["relative_path"]
    subprocess.run(["git", "check-ignore", "-q", str(first)], cwd=ROOT, check=True)
    for record in manifest["files"]:
        path = payload / record["relative_path"]
        assert path.is_file() and not path.is_symlink()
        assert path.stat().st_size == record["size"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]


def test_new_formal_configs_do_not_read_removed_processed_inputs() -> None:
    configs = (
        ROOT / "configs/datasets/fifth_ring.yaml",
        ROOT / "configs/zoning/regularized.yaml",
        ROOT / "configs/pipelines/demand.yaml",
    )
    removed = (
        "data/processed/fifth_ring/segment_nodes.gpkg",
        "data/processed/fifth_ring/segment_relation_edges_road_poi_order.csv",
        "data/processed/fifth_ring/segment_poi_features.csv",
        "data/processed/fifth_ring/segment_order_features.csv",
        "data/processed/fifth_ring/segment_order_od_hourly.csv",
    )
    text = "\n".join(path.read_text() for path in configs)
    assert not any(value in text for value in removed)
    assert "data/interim/fifth_ring/frozen_inputs" in text
