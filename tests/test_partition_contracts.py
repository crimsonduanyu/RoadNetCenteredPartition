from __future__ import annotations

from pathlib import Path
import pickle

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString

from roadnet_partition.config import ResolvedStageConfig
from roadnet_partition.pipeline.results import RunContext, StageStatus
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


def test_run_partition_new_stage_api_writes_only_formal_files_outside_project(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    graph = nx.Graph()
    for left, right, weight in [("a", "b", 5.0), ("b", "c", 1.0), ("c", "d", 5.0)]:
        graph.add_edge(
            left, right, weight=weight,
            continuity_weight=weight, connector_weight=weight,
        )
    graph_path = inputs / "graph.gpickle"
    with graph_path.open("wb") as handle:
        pickle.dump(graph, handle)

    segments = gpd.GeoDataFrame(
        {
            "seg_id": ["a", "b", "c", "d"],
            "length": [1.0, 1.0, 1.0, 1.0],
        },
        geometry=[
            LineString([(0, 0), (1, 0)]),
            LineString([(1, 0), (2, 0)]),
            LineString([(2, 0), (3, 0)]),
            LineString([(3, 0), (4, 0)]),
        ],
        crs="EPSG:3857",
    )
    segment_path = inputs / "segments.gpkg"
    segments.to_file(segment_path, driver="GPKG")
    baseline_path = inputs / "baseline.gpkg"
    segments.assign(cluster_id=[0, 0, 1, 1]).to_file(baseline_path, driver="GPKG")
    order_path = inputs / "orders.csv"
    pd.DataFrame({"seg_id": ["a", "b", "c", "d"], "order_total": [1, 1, 5, 5]}).to_csv(order_path, index=False)
    relation_path = inputs / "relations.csv"
    pd.DataFrame({"seg_id_a": ["a"], "seg_id_b": ["b"]}).to_csv(relation_path, index=False)

    config_path = tmp_path / "configs" / "partition.yaml"
    config_path.parent.mkdir()
    config = ResolvedStageConfig(
        config_path,
        {
            "scope": {"active": "tiny", "graph_variant": "road"},
            "inputs": {
                "graph": graph_path,
                "relation_edges": relation_path,
                "segment_nodes": segment_path,
                "order_features": order_path,
                "baseline_clusters": {"leiden": baseline_path},
            },
            "outputs": {"root": "ignored", "overwrite": True, "resume": False},
            "initializations": ["leiden"],
            "objective": {
                "target_clusters": 2,
                "capacity_loss": "squared_hinge",
                "capacity_min_ratio": 0.5,
                "capacity_max_ratio": 1.5,
                "lambda_g": 1.0,
                "lambda_r": 1.0,
                "alpha_cont": 1.0,
                "alpha_conn": 1.0,
                "grid": {"lambda_c": [1.0]},
            },
            "search": {
                "max_passes": 1,
                "min_delta": 1.0e-9,
                "move_policy": "best_improving",
                "enforce_connectivity": True,
                "allow_merge_split": False,
                "grid": {"merge_split_enabled": [False]},
            },
            "evaluation": {},
        },
        "fingerprint",
    )
    context = RunContext("tiny", tmp_path / "external-run", tmp_path).for_stage("partition")

    result = partition.run_partition(config, context)

    assert result.status is StageStatus.COMPLETE
    assert set(result.outputs) == {
        "resolved_config", "manifest", "objective_trace",
        "cluster_gpkg_regularized_leiden_lc1p0_lr1p0",
        "cluster_csv_regularized_leiden_lc1p0_lr1p0",
    }
    assert all(path.is_file() and path.is_relative_to(context.stage_dir) for path in result.outputs.values())
    assert not (context.stage_dir / "_SUCCESS").exists()
    manifest = pd.read_csv(result.outputs["manifest"])
    assert Path(manifest.loc[0, "clusters_gpkg"]).is_absolute()
