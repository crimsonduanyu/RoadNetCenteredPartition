from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from roadnet_partition.downstream.demand_contracts import (
    validate_cluster_index,
    validate_graph_assets,
    validate_od_and_tensor,
)


def test_cluster_index_contract_handles_numeric_text_and_isolated_cluster() -> None:
    frame = pd.DataFrame(
        {
            "cluster_index": [0, 1, 2],
            "cluster_id": ["2", "10", "isolated"],
            "num_segments": [1, 1, 1],
            "total_length_m": [1.0, 1.0, 1.0],
            "centroid_x": [0.0, 1.0, 2.0],
            "centroid_y": [0.0, 0.0, 0.0],
            "centroid_lon": [0.0, 1.0, 2.0],
            "centroid_lat": [0.0, 0.0, 0.0],
        }
    )
    assert validate_cluster_index(frame, ["isolated", "10", "2"]) == ["2", "10", "isolated"]
    bad = frame.copy()
    bad.loc[2, "cluster_index"] = 3
    with pytest.raises(ValueError, match="continuous"):
        validate_cluster_index(bad)


def test_od_tensor_contract_preserves_axis_direction_and_integer_counts(tmp_path: Path) -> None:
    od = pd.DataFrame(
        [{
            "slot_start": "2020-01-01 00:00:00",
            "origin_cluster_id": "2",
            "destination_cluster_id": "isolated",
            "exclusive_count": 2,
            "carpool_count": 1,
            "total_count": 3,
        }]
    )
    od_path = tmp_path / "cluster_od_10min.csv"
    tensor_path = tmp_path / "od_tensor_10min.npz"
    od.to_csv(od_path, index=False)
    exclusive = np.zeros((1, 3, 3), dtype=np.int32)
    carpool = np.zeros_like(exclusive)
    exclusive[0, 0, 2] = 2
    carpool[0, 0, 2] = 1
    np.savez_compressed(
        tensor_path,
        Y_exclusive=exclusive,
        Y_carpool=carpool,
        Y_total=exclusive + carpool,
        slot_start=np.array(["2020-01-01 00:00:00"]),
        cluster_ids=np.array(["2", "10", "isolated"]),
    )
    result = validate_od_and_tensor(od_path, tensor_path, ["2", "10", "isolated"])
    assert result["shape"] == (1, 3, 3)
    assert result["sums"] == {"exclusive": 2, "carpool": 1, "total": 3}


def test_graph_contract_keeps_isolated_cluster_matrix_rows(tmp_path: Path) -> None:
    edges = pd.DataFrame([{
        "cluster_id_a": "2", "cluster_id_b": "10",
        "cluster_index_a": 0, "cluster_index_b": 1, "weight": 2.0,
    }])
    edges.to_csv(tmp_path / "cluster_graph_road_edges.csv", index=False)
    raw = sparse.csr_matrix(np.array([[0.0, 2.0, 0.0], [2.0, 0.0, 0.0], [0.0, 0.0, 0.0]]))
    work = raw + sparse.eye(3, format="csr")
    degree = np.asarray(work.sum(axis=1)).ravel()
    scale = sparse.diags(1.0 / np.sqrt(degree))
    normalized = scale @ work @ scale
    sparse.save_npz(tmp_path / "cluster_graph_road_adjacency_raw.npz", raw)
    sparse.save_npz(tmp_path / "cluster_graph_road_adjacency_normalized.npz", normalized)
    result = validate_graph_assets(tmp_path, "road", 3, add_self_loops=True, symmetric=True)
    assert result["endpoint_nodes"] == 2
    assert result["shape"] == (3, 3)
