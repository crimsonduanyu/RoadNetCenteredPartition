from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

import geopandas as gpd


REQUIRED_PARTITION_COLUMNS = ("seg_id", "cluster_id", "geometry")


def partition_mapping(clusters: gpd.GeoDataFrame) -> dict[str, Any]:
    return dict(zip(clusters["seg_id"].astype(str), clusters["cluster_id"]))


def partition_groups(clusters: gpd.GeoDataFrame) -> set[frozenset[str]]:
    groups: dict[Any, set[str]] = {}
    for seg_id, cluster_id in partition_mapping(clusters).items():
        groups.setdefault(cluster_id, set()).add(seg_id)
    return {frozenset(nodes) for nodes in groups.values()}


def compare_partitions(
    actual: gpd.GeoDataFrame,
    expected: gpd.GeoDataFrame,
    *,
    strict_mapping: bool = False,
) -> bool:
    if strict_mapping:
        return partition_mapping(actual) == partition_mapping(expected)
    return partition_groups(actual) == partition_groups(expected)


def validate_partition(
    clusters: gpd.GeoDataFrame,
    *,
    expected_segment_ids: Iterable[str] | None = None,
    expected_crs: Any | None = None,
    expected_bounds: Iterable[float] | None = None,
    required_columns: Iterable[str] = REQUIRED_PARTITION_COLUMNS,
    expected_dtypes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    missing_columns = [column for column in required_columns if column not in clusters.columns]
    if missing_columns:
        raise ValueError(f"Partition is missing required columns: {missing_columns}")
    segment_ids = clusters["seg_id"].astype(str)
    if segment_ids.duplicated().any():
        duplicates = segment_ids.loc[segment_ids.duplicated()].tolist()[:5]
        raise ValueError(f"Partition contains duplicate segment IDs: {duplicates}")
    if clusters["cluster_id"].isna().any():
        raise ValueError("Partition contains null cluster IDs.")
    if expected_segment_ids is not None:
        expected = {str(value) for value in expected_segment_ids}
        actual = set(segment_ids)
        if actual != expected:
            raise ValueError(
                f"Partition segment coverage differs: missing={sorted(expected - actual)[:5]}, "
                f"extra={sorted(actual - expected)[:5]}"
            )
    if expected_crs is not None and clusters.crs != expected_crs:
        raise ValueError(f"Partition CRS differs: actual={clusters.crs}, expected={expected_crs}")
    if clusters.geometry.isna().any() or clusters.geometry.is_empty.any():
        raise ValueError("Partition contains null or empty geometry.")
    if not clusters.geometry.is_valid.all():
        raise ValueError("Partition contains invalid geometry.")
    bounds = tuple(float(value) for value in clusters.total_bounds)
    if expected_bounds is not None and bounds != tuple(float(value) for value in expected_bounds):
        raise ValueError(f"Partition bounds differ: actual={bounds}, expected={tuple(expected_bounds)}")
    if expected_dtypes:
        actual_dtypes = {column: str(clusters[column].dtype) for column in expected_dtypes}
        if actual_dtypes != dict(expected_dtypes):
            raise ValueError(f"Partition dtypes differ: actual={actual_dtypes}, expected={dict(expected_dtypes)}")
    return {
        "segment_count": len(segment_ids),
        "cluster_count": int(clusters["cluster_id"].nunique()),
        "crs": str(clusters.crs),
        "bounds": bounds,
        "columns": list(clusters.columns),
        "dtypes": {column: str(dtype) for column, dtype in clusters.dtypes.items()},
    }


def validate_cluster_index(cluster_ids: Iterable[Any], graph_nodes: Iterable[Any]) -> None:
    index_nodes = set(cluster_ids)
    nodes = set(graph_nodes)
    if index_nodes != nodes:
        raise ValueError(
            f"Cluster index and graph nodes differ: missing={sorted(nodes - index_nodes)[:5]}, "
            f"extra={sorted(index_nodes - nodes)[:5]}"
        )


def save_partition(
    output_path: Path,
    csv_path: Path,
    base_segments: gpd.GeoDataFrame,
    partition: dict[str, int],
    initialization: str,
    current_setting_id: str,
    overwrite: bool,
) -> None:
    if output_path.exists() and overwrite:
        output_path.unlink()
    if csv_path.exists() and overwrite:
        csv_path.unlink()
    segments = base_segments.copy()
    segments["seg_id"] = segments["seg_id"].astype(str)
    segments["cluster_id"] = segments["seg_id"].map(partition)
    if segments["cluster_id"].isna().any():
        raise ValueError(f"Output partition is missing labels for {int(segments['cluster_id'].isna().sum())} segments.")
    segments["regularized_init"] = initialization
    segments["setting_id"] = current_setting_id
    segments.to_file(output_path, driver="GPKG")
    segments.drop(columns="geometry").to_csv(csv_path, index=False)
