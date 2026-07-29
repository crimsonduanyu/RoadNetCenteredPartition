from __future__ import annotations

import hashlib
import json
from pathlib import Path

import geopandas as gpd
import pytest

from roadnet_partition.zoning.contracts import partition_groups, validate_partition


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def digest_lines(lines) -> str:
    digest = hashlib.sha256()
    for line in lines:
        digest.update(str(line).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def test_phase0_canonical_partition_contract_read_only() -> None:
    baseline = json.loads((PROJECT_ROOT / "docs/refactor/pre-refactor-v1/semantic-baseline.json").read_text())["partition"]
    path = PROJECT_ROOT / baseline["canonical_path"]
    if not path.exists():
        pytest.skip("private Phase 0 canonical asset is not present")
    clusters = gpd.read_file(path)
    summary = validate_partition(
        clusters,
        expected_segment_ids=clusters["seg_id"].astype(str),
        expected_crs=baseline["crs"],
        expected_bounds=baseline["geometry"]["bounds"],
        required_columns=baseline["columns"],
    )
    groups = sorted((sorted(group) for group in partition_groups(clusters)), key=lambda members: (len(members), members))
    assert summary["segment_count"] == baseline["segment_count"]
    assert summary["cluster_count"] == baseline["cluster_count"]
    assert digest_lines("\t".join(members) for members in groups) == baseline["cluster_grouping_sha256"]
