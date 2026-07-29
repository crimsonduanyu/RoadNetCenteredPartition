from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


MATRICES = [
    "TTE_raw.parquet",
    "TTE_count.parquet",
    "TTE_support.parquet",
    "TTE_hops.parquet",
    "TTE_imputed.parquet",
]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def batch_hash(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    digest.update(np.asarray(frame.index).view("int64").tobytes())
    digest.update(str(frame.to_numpy().dtype).encode())
    digest.update(np.ascontiguousarray(frame.to_numpy()).tobytes())
    return digest.hexdigest()


def compare_matrix(left_path: Path, right_path: Path, batch_size: int) -> dict:
    left_file = pq.ParquetFile(left_path)
    right_file = pq.ParquetFile(right_path)
    schema_equal = left_file.schema_arrow.equals(right_file.schema_arrow, check_metadata=True)
    metadata_equal = (
        left_file.metadata.num_rows == right_file.metadata.num_rows
        and left_file.metadata.num_columns == right_file.metadata.num_columns
        and left_file.metadata.num_row_groups == right_file.metadata.num_row_groups
    )
    mismatch_count = 0
    mask_mismatch_count = 0
    max_absolute_error = 0.0
    max_relative_error = 0.0
    error_threshold_counts = {"gt_0": 0, "gt_1e-12": 0, "gt_1e-9": 0, "gt_1e-6": 0}
    first_difference = None
    chunks = []
    row_offset = 0
    left_batches = left_file.iter_batches(batch_size=batch_size)
    right_batches = right_file.iter_batches(batch_size=batch_size)
    for chunk_id, (left_batch, right_batch) in enumerate(zip(left_batches, right_batches, strict=True)):
        left = left_batch.to_pandas()
        right = right_batch.to_pandas()
        axes_equal = left.index.equals(right.index) and left.columns.equals(right.columns)
        if not axes_equal:
            raise ValueError(f"matrix axes differ in chunk {chunk_id}: {left_path.name}")
        left_values = left.to_numpy()
        right_values = right.to_numpy()
        left_mask = pd.isna(left_values)
        right_mask = pd.isna(right_values)
        mask_difference = left_mask ^ right_mask
        chunk_mask_mismatch = int(mask_difference.sum())
        mask_mismatch_count += chunk_mask_mismatch
        comparable = ~(left_mask | right_mask)
        if np.issubdtype(left_values.dtype, np.floating):
            differences = np.abs(left_values[comparable] - right_values[comparable])
            chunk_mismatch = int(np.count_nonzero(differences)) + chunk_mask_mismatch
            if differences.size:
                chunk_max_absolute = float(differences.max())
                denominator = np.maximum(
                    np.maximum(np.abs(left_values[comparable]), np.abs(right_values[comparable])),
                    np.finfo(left_values.dtype).tiny,
                )
                relative = differences / denominator
                chunk_max_relative = float(relative.max())
                max_absolute_error = max(max_absolute_error, chunk_max_absolute)
                max_relative_error = max(max_relative_error, chunk_max_relative)
                error_threshold_counts["gt_0"] += int((differences > 0).sum())
                error_threshold_counts["gt_1e-12"] += int((differences > 1e-12).sum())
                error_threshold_counts["gt_1e-9"] += int((differences > 1e-9).sum())
                error_threshold_counts["gt_1e-6"] += int((differences > 1e-6).sum())
        else:
            difference = left_values != right_values
            chunk_mismatch = int(difference.sum())
            if chunk_mismatch:
                max_absolute_error = max(
                    max_absolute_error,
                    float(np.abs(left_values[difference].astype("int64") - right_values[difference].astype("int64")).max()),
                )
        mismatch_count += chunk_mismatch
        if first_difference is None and chunk_mismatch:
            difference = mask_difference | ((left_values != right_values) & comparable)
            row, column = np.argwhere(difference)[0]
            first_difference = {
                "time": str(left.index[row]),
                "od": str(left.columns[column]),
                "left": None if left_mask[row, column] else left_values[row, column].item(),
                "right": None if right_mask[row, column] else right_values[row, column].item(),
            }
        chunks.append({
            "chunk": chunk_id,
            "row_start": row_offset,
            "row_count": len(left),
            "left_sha256": batch_hash(left),
            "right_sha256": batch_hash(right),
            "mismatch_count": chunk_mismatch,
            "mask_mismatch_count": chunk_mask_mismatch,
        })
        row_offset += len(left)
    return {
        "schema_equal": schema_equal,
        "metadata_shape_equal": metadata_equal,
        "rows": row_offset,
        "columns": left_file.metadata.num_columns - 1,
        "dtype": str(next(left_file.iter_batches(batch_size=1)).to_pandas().to_numpy().dtype),
        "file_sha256_equal": file_sha256(left_path) == file_sha256(right_path),
        "mismatch_count": mismatch_count,
        "mask_mismatch_count": mask_mismatch_count,
        "max_absolute_error": max_absolute_error,
        "max_relative_error": max_relative_error,
        "error_threshold_counts": error_threshold_counts,
        "first_difference": first_difference,
        "chunks": chunks,
    }


def compare_small_assets(left: Path, right: Path) -> dict:
    left_distance = pd.read_parquet(left / "cluster_network_distance.parquet")
    right_distance = pd.read_parquet(right / "cluster_network_distance.parquet")
    distance_axes_equal = left_distance.index.equals(right_distance.index) and left_distance.columns.equals(right_distance.columns)
    distance_error = np.abs(left_distance.to_numpy() - right_distance.to_numpy())
    left_reps = pd.read_csv(left / "cluster_representative_nodes.csv")
    right_reps = pd.read_csv(right / "cluster_representative_nodes.csv")
    return {
        "network_distance": {
            "axes_equal": distance_axes_equal,
            "mask_mismatch_count": int((pd.isna(left_distance.to_numpy()) ^ pd.isna(right_distance.to_numpy())).sum()),
            "max_absolute_error": float(np.nanmax(distance_error)),
            "file_sha256_equal": file_sha256(left / "cluster_network_distance.parquet") == file_sha256(right / "cluster_network_distance.parquet"),
        },
        "representative_nodes": {
            "exact": left_reps.equals(right_reps),
            "file_sha256_equal": file_sha256(left / "cluster_representative_nodes.csv") == file_sha256(right / "cluster_representative_nodes.csv"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream-compare two complete TTE output directories.")
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()
    result = {
        "left": str(args.left.resolve()),
        "right": str(args.right.resolve()),
        "batch_size": args.batch_size,
        "matrices": {
            filename: compare_matrix(args.left / filename, args.right / filename, args.batch_size)
            for filename in MATRICES
        },
        **compare_small_assets(args.left, args.right),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    summary = {
        filename: {
            key: value
            for key, value in metrics.items()
            if key not in {"chunks"}
        }
        for filename, metrics in result["matrices"].items()
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
