from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


DTYPES = {
    "raw": "float64",
    "count": "int32",
    "support": "int32",
    "hops": "int16",
    "imputed": "float32",
}


def _parquet_columns(parquet: pq.ParquetFile) -> tuple[list[str], str | None, str | None]:
    metadata = parquet.schema_arrow.pandas_metadata
    if not metadata or len(metadata.get("index_columns", [])) != 1:
        raise ValueError("TTE parquet must contain one pandas index")
    index_field = metadata["index_columns"][0]
    columns = [
        str(column["name"])
        for column in metadata["columns"]
        if column["field_name"] != index_field
    ]
    column_indexes = metadata.get("column_indexes", [])
    columns_name = column_indexes[0].get("name") if len(column_indexes) == 1 else None
    return columns, columns_name, index_field


def _cluster_axis(columns: Iterable[str]) -> list[str]:
    columns = list(columns)
    pairs = []
    for column in columns:
        parts = column.split("->")
        if len(parts) != 2:
            raise ValueError(f"invalid TTE OD column: {column!r}")
        pairs.append((parts[0], parts[1]))
    clusters = list(dict.fromkeys(origin for origin, _ in pairs))
    if columns != [f"{origin}->{destination}" for origin in clusters for destination in clusters]:
        raise ValueError("TTE columns are not a complete origin-major cluster cartesian product")
    return clusters


def _validate_distance_assets(
    output_dir: Path,
    clusters: list[str],
    matrix_filename: str,
    representatives_filename: str,
) -> dict[str, Any]:
    matrix = pd.read_parquet(output_dir / matrix_filename)
    matrix.index = matrix.index.astype(str)
    matrix.columns = matrix.columns.astype(str)
    if list(matrix.index) != clusters or list(matrix.columns) != clusters:
        raise ValueError("network-distance axes differ from the TTE cluster axis")
    if not all(dtype == np.dtype("float64") for dtype in matrix.dtypes):
        raise ValueError("network-distance matrix must be float64")
    values = matrix.to_numpy()
    if not np.allclose(values, values.T, rtol=0.0, atol=0.0, equal_nan=True):
        raise ValueError("network-distance matrix is not exactly symmetric")
    if not np.array_equal(np.diag(values), np.zeros(len(clusters))):
        raise ValueError("network-distance diagonal is not zero")

    representatives = pd.read_csv(output_dir / representatives_filename, dtype={"cluster_id": str})
    expected_columns = ["cluster_id", "rep_osmid", "dist_to_centroid_m"]
    if list(representatives.columns) != expected_columns:
        raise ValueError("representative-node schema differs")
    if representatives["cluster_id"].tolist() != clusters or not representatives["rep_osmid"].is_unique:
        raise ValueError("representative-node cluster order or node uniqueness differs")
    return {
        "distance_finite_pairs": int(np.isfinite(values).sum()),
        "distance_unreachable_pairs": int(np.isinf(values).sum()),
    }


def validate_tte_outputs(
    output_dir: Path,
    *,
    count_filename: str = "TTE_count.parquet",
    support_filename: str = "TTE_support.parquet",
    hops_filename: str = "TTE_hops.parquet",
    matrix_filename: str = "cluster_network_distance.parquet",
    representatives_filename: str = "cluster_representative_nodes.csv",
    expected_cluster_ids: Iterable[Any] | None = None,
    expected_time_index: pd.DatetimeIndex | None = None,
    raw_range: tuple[float, float] | None = None,
    max_hops: int | None = None,
    batch_size: int = 64,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    filenames = {
        "raw": "TTE_raw.parquet",
        "count": count_filename,
        "support": support_filename,
        "hops": hops_filename,
        "imputed": "TTE_imputed.parquet",
    }
    required = list(filenames.values()) + [matrix_filename, representatives_filename]
    missing = [name for name in required if not (output_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"TTE contract files are missing: {missing}")

    parquets = {name: pq.ParquetFile(output_dir / filename) for name, filename in filenames.items()}
    reference_columns = None
    reference_rows = None
    for name, parquet in parquets.items():
        columns, columns_name, _ = _parquet_columns(parquet)
        if columns_name != "OD":
            raise ValueError(f"{filenames[name]} columns axis name differs")
        if reference_columns is None:
            reference_columns = columns
            reference_rows = parquet.metadata.num_rows
        elif columns != reference_columns or parquet.metadata.num_rows != reference_rows:
            raise ValueError("TTE matrix axes or shape differ")
        metadata = parquet.schema_arrow.pandas_metadata
        numpy_types = {
            column["numpy_type"]
            for column in metadata["columns"]
            if column["field_name"] not in metadata["index_columns"]
        }
        if numpy_types != {DTYPES[name]}:
            raise ValueError(f"{filenames[name]} dtype differs: {sorted(numpy_types)}")

    assert reference_columns is not None and reference_rows is not None
    clusters = _cluster_axis(reference_columns)
    if expected_cluster_ids is not None and clusters != [str(value) for value in expected_cluster_ids]:
        raise ValueError("TTE cluster order differs from expected cluster ids")
    distance_stats = _validate_distance_assets(
        output_dir, clusters, matrix_filename, representatives_filename
    )

    observed_cells = 0
    inferred_cells = 0
    missing_cells = 0
    diagonal_observed_cells = 0
    diagonal_missing_cells = 0
    count_total = 0
    hop_distribution: Counter[int] = Counter()
    previous_time = None
    offset = 0
    diagonal = np.array([column.split("->")[0] == column.split("->")[1] for column in reference_columns])
    iterators = [parquets[name].iter_batches(batch_size=batch_size) for name in filenames]
    for batches in zip(*iterators, strict=True):
        frames = {name: batch.to_pandas() for name, batch in zip(filenames, batches)}
        index = frames["raw"].index
        if index.name is not None or any(not frame.index.equals(index) for frame in frames.values()):
            raise ValueError("TTE matrix time indexes differ")
        if not index.is_unique or not index.is_monotonic_increasing:
            raise ValueError("TTE time index must be unique and increasing")
        if len(index) and previous_time is not None and index[0] <= previous_time:
            raise ValueError("TTE time index is not increasing across batches")
        if len(index):
            previous_time = index[-1]
        if expected_time_index is not None and not index.equals(expected_time_index[offset:offset + len(index)]):
            raise ValueError("TTE time index differs from configured range")
        offset += len(index)

        raw = frames["raw"].to_numpy()
        count = frames["count"].to_numpy()
        support = frames["support"].to_numpy()
        hops = frames["hops"].to_numpy()
        imputed = frames["imputed"].to_numpy()
        observed = ~np.isnan(raw)
        inferred = hops >= 1
        missing_mask = np.isnan(imputed)
        if not np.array_equal(count > 0, observed) or np.any(count < 0):
            raise ValueError("TTE count/raw observed-mask relation differs")
        if not np.array_equal(hops == 0, observed):
            raise ValueError("TTE hops/raw observed-mask relation differs")
        if not np.array_equal(inferred, ~observed & ~missing_mask):
            raise ValueError("TTE hops/imputed inferred-mask relation differs")
        if not np.array_equal(hops == -1, missing_mask) or np.any(hops < -1):
            raise ValueError("TTE hops/unfilled relation differs")
        if max_hops is not None and np.any(hops > max_hops):
            raise ValueError("TTE hops exceed configured max_hops")
        if not np.array_equal(support > 0, inferred) or np.any(support[~inferred] != -1):
            raise ValueError("TTE support/inferred relation differs")
        if not np.array_equal(imputed[observed], raw.astype(np.float32)[observed]):
            raise ValueError("TTE imputed values do not preserve observed raw values as float32")
        if raw_range is not None and observed.any():
            low, high = raw_range
            if np.any(raw[observed] < low) or np.any(raw[observed] > high):
                raise ValueError("TTE raw observations fall outside the configured trip-time range")
        if np.any(inferred[:, diagonal]):
            raise ValueError("TTE diagonal contains newly imputed values")

        observed_cells += int(observed.sum())
        inferred_cells += int(inferred.sum())
        missing_cells += int(missing_mask.sum())
        diagonal_observed_cells += int(observed[:, diagonal].sum())
        diagonal_missing_cells += int(missing_mask[:, diagonal].sum())
        count_total += int(count.sum())
        values, counts = np.unique(hops, return_counts=True)
        hop_distribution.update({int(value): int(count_value) for value, count_value in zip(values, counts)})

    if offset != reference_rows or (expected_time_index is not None and offset != len(expected_time_index)):
        raise ValueError("TTE time-axis length differs")
    if observed_cells + inferred_cells + missing_cells != reference_rows * len(reference_columns):
        raise ValueError("TTE observed/inferred/missing masks do not partition the matrix")
    return {
        "shape": [reference_rows, len(reference_columns)],
        "clusters": clusters,
        "observed_cells": observed_cells,
        "inferred_cells": inferred_cells,
        "missing_cells": missing_cells,
        "count_total": count_total,
        "hop_distribution": dict(sorted(hop_distribution.items())),
        "diagonal_observed_cells": diagonal_observed_cells,
        "diagonal_missing_cells": diagonal_missing_cells,
        **distance_stats,
    }
