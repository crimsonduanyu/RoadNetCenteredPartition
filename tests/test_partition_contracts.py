from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString

from roadnet_partition.pipeline.results import StageStatus
from roadnet_partition.zoning import partition
from roadnet_partition.zoning.contracts import (
    compare_partitions,
    partition_groups,
    validate_cluster_index,
    validate_partition,
)


def frame(labels=(10, 10, 20)) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"seg_id": ["a", "b", "isolated"], "cluster_id": list(labels), "length": [1.0, 1.0, 1.0]},
        geometry=[
            LineString([(0, 0), (1, 0)]),
            LineString([(1, 0), (2, 0)]),
            LineString([(5, 0), (6, 0)]),
        ],
        crs="EPSG:3857",
    )


def test_partition_contract_accepts_label_invariant_grouping() -> None:
    actual = frame((10, 10, 20))
    relabeled = frame((1, 1, 0))
    assert partition_groups(actual) == {frozenset({"a", "b"}), frozenset({"isolated"})}
    assert compare_partitions(actual, relabeled)
    assert not compare_partitions(actual, relabeled, strict_mapping=True)
    summary = validate_partition(
        actual,
        expected_segment_ids=["a", "b", "isolated"],
        expected_crs=actual.crs,
        expected_bounds=actual.total_bounds,
        expected_dtypes={"seg_id": str(actual["seg_id"].dtype), "cluster_id": str(actual["cluster_id"].dtype)},
    )
    assert summary["cluster_count"] == 2


def test_partition_contract_rejects_duplicate_missing_and_null_labels() -> None:
    duplicate = frame()
    duplicate.loc[1, "seg_id"] = "a"
    with pytest.raises(ValueError, match="duplicate"):
        validate_partition(duplicate)
    with pytest.raises(ValueError, match="coverage"):
        validate_partition(frame(), expected_segment_ids=["a", "b"])
    null_label = frame()
    null_label.loc[0, "cluster_id"] = None
    with pytest.raises(ValueError, match="null cluster"):
        validate_partition(null_label)


def test_cluster_index_must_include_isolated_nodes_explicitly() -> None:
    validate_cluster_index([0, 1, 2], [0, 1, 2])
    with pytest.raises(ValueError, match="differ"):
        validate_cluster_index([0, 1], [0, 1, 2])


def test_run_partition_forces_explicit_output_root(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(partition, "validate_config", lambda config: calls.append(("validate", config)))
    monkeypatch.setattr(partition, "run_from_config", lambda config, path: calls.append(("run", config, path)))
    output_root = tmp_path / "partition"
    result = partition.run_partition(
        {"outputs": {"root": "must-not-be-used"}},
        output_root,
        tmp_path / "fixture.yaml",
    )
    assert result.status is StageStatus.COMPLETE
    assert result.outputs["root"] == output_root.resolve()
    assert calls[0][1]["outputs"]["root"] == str(output_root.resolve())
    assert calls[1][1]["outputs"]["root"] == str(output_root.resolve())
