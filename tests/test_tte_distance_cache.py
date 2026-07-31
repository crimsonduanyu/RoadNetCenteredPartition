from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from roadnet_partition.graphs import distance as network_distance


def _write_valid(base: Path) -> None:
    pd.DataFrame(
        [[0.0, 5000.0], [5000.0, 0.0]],
        index=["1", "2"], columns=["1", "2"],
    ).to_parquet(base / "cluster_network_distance.parquet")
    pd.DataFrame(
        {"cluster_id": ["1", "2"], "rep_osmid": [101, 202], "dist_to_centroid_m": [1.0, 2.0]}
    ).to_csv(base / "cluster_representative_nodes.csv", index=False)


def test_load_validated_distance_loads_valid_cache(tmp_path: Path) -> None:
    _write_valid(tmp_path)
    matrix = network_distance._load_validated_distance(
        tmp_path / "cluster_network_distance.parquet",
        tmp_path / "cluster_representative_nodes.csv",
    )
    assert matrix is not None
    assert list(matrix.index) == ["1", "2"]
    assert matrix.shape == (2, 2)


def test_load_validated_distance_returns_none_when_absent(tmp_path: Path) -> None:
    assert network_distance._load_validated_distance(
        tmp_path / "missing.parquet", tmp_path / "missing.csv"
    ) is None


def test_load_validated_distance_loads_matrix_when_reps_absent(tmp_path: Path) -> None:
    # reps is validated only when present (production writes matrix+reps together);
    # a caller that pre-places only the matrix still gets it loaded.
    pd.DataFrame([[0.0, 1.0], [1.0, 0.0]], index=["1", "2"], columns=["1", "2"]).to_parquet(
        tmp_path / "cluster_network_distance.parquet"
    )
    matrix = network_distance._load_validated_distance(
        tmp_path / "cluster_network_distance.parquet", tmp_path / "missing.csv"
    )
    assert matrix is not None
    assert matrix.shape == (2, 2)


def test_load_validated_distance_returns_none_when_asymmetric(tmp_path: Path) -> None:
    pd.DataFrame(
        [[0.0, 5000.0], [9999.0, 0.0]],
        index=["1", "2"], columns=["1", "2"],
    ).to_parquet(tmp_path / "cluster_network_distance.parquet")
    pd.DataFrame({"cluster_id": ["1", "2"], "rep_osmid": [1, 2], "dist_to_centroid_m": [0.0, 0.0]}).to_csv(
        tmp_path / "cluster_representative_nodes.csv", index=False
    )
    assert network_distance._load_validated_distance(
        tmp_path / "cluster_network_distance.parquet",
        tmp_path / "cluster_representative_nodes.csv",
    ) is None


def test_load_validated_distance_returns_none_when_reps_mismatch(tmp_path: Path) -> None:
    _write_valid(tmp_path)
    # overwrite reps with a different cluster set
    pd.DataFrame({"cluster_id": ["1", "9"], "rep_osmid": [1, 2], "dist_to_centroid_m": [0.0, 0.0]}).to_csv(
        tmp_path / "cluster_representative_nodes.csv", index=False
    )
    assert network_distance._load_validated_distance(
        tmp_path / "cluster_network_distance.parquet",
        tmp_path / "cluster_representative_nodes.csv",
    ) is None
