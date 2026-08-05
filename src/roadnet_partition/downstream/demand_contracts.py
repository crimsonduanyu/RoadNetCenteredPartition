from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from scipy import sparse

from roadnet_partition.downstream.order_checkpoints import (
    identifier_csv_options,
    invalid_driver_identifiers,
    normalize_driver_identifiers,
    normalize_order_identifiers,
)


ASSIGNED_COLUMNS = [
    "stage_id", "source_file", "source_row", "order_id", "driver_id",
    "departure_time", "finish_time", "slot_start", "pickup_seg_id",
    "dropoff_seg_id", "origin_cluster_id", "destination_cluster_id",
    "pickup_match_distance_m", "dropoff_match_distance_m", "service_type",
]
CLUSTER_INDEX_COLUMNS = [
    "cluster_index", "cluster_id", "num_segments", "total_length_m",
    "centroid_x", "centroid_y", "centroid_lon", "centroid_lat",
]
OD_COLUMNS = [
    "slot_start", "origin_cluster_id", "destination_cluster_id",
    "exclusive_count", "carpool_count", "total_count",
]
SERVICE_TYPES = {"exclusive", "carpool"}


def _sort_cluster_ids(values: Iterable[Any]) -> list[str]:
    def key(value: Any) -> tuple[int, int | str]:
        text = str(value)
        try:
            return (0, int(text))
        except ValueError:
            return (1, text)
    return sorted({str(value) for value in values}, key=key)


def validate_cluster_index(frame: pd.DataFrame, expected_cluster_ids: Iterable[Any] | None = None) -> list[str]:
    missing = [column for column in CLUSTER_INDEX_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"cluster_index is missing columns: {missing}")
    if frame["cluster_id"].astype(str).duplicated().any():
        raise ValueError("cluster_index contains duplicate cluster IDs")
    if frame["cluster_index"].duplicated().any():
        raise ValueError("cluster_index contains duplicate indices")
    indices = frame["cluster_index"].astype(int).tolist()
    if indices != list(range(len(frame))):
        raise ValueError("cluster_index indices must be continuous and row-ordered")
    cluster_ids = frame["cluster_id"].astype(str).tolist()
    if cluster_ids != _sort_cluster_ids(cluster_ids):
        raise ValueError("cluster_index cluster IDs use the wrong ordering")
    if expected_cluster_ids is not None and cluster_ids != _sort_cluster_ids(expected_cluster_ids):
        raise ValueError("cluster_index does not match the Partition cluster set")
    return cluster_ids


def validate_assigned_orders(
    path: Path,
    cluster_ids: Iterable[str],
    *,
    expected_rows: int | None = None,
    expected_start: str | None = None,
    expected_end: str | None = None,
    chunksize: int = 500_000,
) -> dict[str, Any]:
    clusters = set(str(value) for value in cluster_ids)
    rows = 0
    services: set[str] = set()
    stage_min = None
    stage_max = None
    previous_stage = 0
    min_departure = None
    max_departure = None
    options = identifier_csv_options()
    options["dtype"].update({
        "origin_cluster_id": str,
        "destination_cluster_id": str,
        "service_type": str,
    })
    for chunk in pd.read_csv(path, chunksize=chunksize, **options):
        if list(chunk.columns) != ASSIGNED_COLUMNS:
            raise ValueError(f"assigned-order schema differs: {list(chunk.columns)}")
        chunk["order_id"] = normalize_order_identifiers(chunk["order_id"])
        chunk["driver_id"] = normalize_driver_identifiers(chunk["driver_id"])
        if invalid_driver_identifiers(chunk["driver_id"]).any():
            raise ValueError("assigned orders contain null or blank driver_id values")
        if chunk[["stage_id", "origin_cluster_id", "destination_cluster_id", "service_type"]].isna().any().any():
            raise ValueError("assigned orders contain null required values")
        stages = chunk["stage_id"].astype(np.int64)
        if stages.duplicated().any() or (not stages.empty and int(stages.iloc[0]) <= previous_stage):
            raise ValueError("assigned-order stage_id is not unique and increasing")
        if not stages.empty:
            previous_stage = int(stages.iloc[-1])
            stage_min = int(stages.iloc[0]) if stage_min is None else stage_min
            stage_max = previous_stage
        if not set(chunk["origin_cluster_id"]).issubset(clusters) or not set(chunk["destination_cluster_id"]).issubset(clusters):
            raise ValueError("assigned orders contain a cluster absent from cluster_index")
        services.update(chunk["service_type"].dropna().astype(str))
        departures = pd.to_datetime(chunk["departure_time"], errors="coerce")
        if departures.isna().any():
            raise ValueError("assigned orders contain invalid departure timestamps")
        current_min = departures.min()
        current_max = departures.max()
        min_departure = current_min if min_departure is None else min(min_departure, current_min)
        max_departure = current_max if max_departure is None else max(max_departure, current_max)
        rows += len(chunk)
    if not services.issubset(SERVICE_TYPES):
        raise ValueError(f"assigned orders contain unknown service types: {services - SERVICE_TYPES}")
    if expected_rows is not None and rows != expected_rows:
        raise ValueError(f"assigned-order row count differs: {rows} != {expected_rows}")
    if expected_start is not None and min_departure != pd.Timestamp(expected_start):
        raise ValueError(f"assigned-order minimum departure differs: {min_departure}")
    if expected_end is not None and max_departure != pd.Timestamp(expected_end):
        raise ValueError(f"assigned-order maximum departure differs: {max_departure}")
    return {
        "rows": rows,
        "services": sorted(services),
        "stage_id_min": stage_min,
        "stage_id_max": stage_max,
        "min_departure": None if min_departure is None else str(min_departure),
        "max_departure": None if max_departure is None else str(max_departure),
    }


def validate_assigned_sample(full_path: Path, sample_path: Path) -> None:
    sample = pd.read_parquet(sample_path)
    if not set(sample.columns).issubset(ASSIGNED_COLUMNS):
        raise ValueError("assigned-order sample schema is not derived from the full output")
    sample_ids = set(sample["stage_id"].astype(int))
    found: set[int] = set()
    for chunk in pd.read_csv(full_path, usecols=["stage_id"], chunksize=500_000):
        found.update(sample_ids.intersection(chunk["stage_id"].astype(int)))
        if found == sample_ids:
            break
    if found != sample_ids:
        raise ValueError("assigned-order sample contains rows absent from the full output")


def validate_od_and_tensor(od_path: Path, tensor_path: Path, cluster_ids: list[str]) -> dict[str, Any]:
    od = pd.read_csv(
        od_path,
        dtype={"origin_cluster_id": str, "destination_cluster_id": str},
    )
    if list(od.columns) != OD_COLUMNS:
        raise ValueError(f"OD schema differs: {list(od.columns)}")
    for column in ["exclusive_count", "carpool_count", "total_count"]:
        values = od[column].to_numpy()
        if not np.issubdtype(values.dtype, np.integer):
            raise ValueError(f"OD {column} must be integer")
    if not np.array_equal(
        od["total_count"].to_numpy(),
        od["exclusive_count"].to_numpy() + od["carpool_count"].to_numpy(),
    ):
        raise ValueError("OD total_count differs from service components")
    clusters = set(cluster_ids)
    if not set(od["origin_cluster_id"]).issubset(clusters) or not set(od["destination_cluster_id"]).issubset(clusters):
        raise ValueError("OD endpoints contain a cluster absent from cluster_index")

    with np.load(tensor_path) as tensor:
        required = {"Y_exclusive", "Y_carpool", "Y_total", "slot_start", "cluster_ids"}
        if set(tensor.files) != required:
            raise ValueError(f"tensor members differ: {tensor.files}")
        tensor_clusters = tensor["cluster_ids"].astype(str).tolist()
        if tensor_clusters != cluster_ids:
            raise ValueError("tensor cluster axis differs from cluster_index")
        shape = (len(tensor["slot_start"]), len(cluster_ids), len(cluster_ids))
        for name in ["Y_exclusive", "Y_carpool", "Y_total"]:
            if tensor[name].shape != shape or tensor[name].dtype != np.int32:
                raise ValueError(f"tensor {name} shape or dtype differs")
        if not np.array_equal(tensor["Y_total"], tensor["Y_exclusive"] + tensor["Y_carpool"]):
            raise ValueError("Y_total differs from service tensors")
        sums = {
            "exclusive": int(tensor["Y_exclusive"].sum()),
            "carpool": int(tensor["Y_carpool"].sum()),
            "total": int(tensor["Y_total"].sum()),
        }
        nonzero = {
            "exclusive": int(np.count_nonzero(tensor["Y_exclusive"])),
            "carpool": int(np.count_nonzero(tensor["Y_carpool"])),
            "total": int(np.count_nonzero(tensor["Y_total"])),
        }
        slots = tensor["slot_start"].astype(str)
    csv_sums = {
        "exclusive": int(od["exclusive_count"].sum()),
        "carpool": int(od["carpool_count"].sum()),
        "total": int(od["total_count"].sum()),
    }
    if sums != csv_sums:
        raise ValueError(f"OD CSV and tensor sums differ: {csv_sums} != {sums}")
    return {
        "rows": len(od), "shape": shape, "sums": sums, "nonzero": nonzero,
        "slot_first": None if len(slots) == 0 else slots[0],
        "slot_last": None if len(slots) == 0 else slots[-1],
    }


def validate_graph_assets(output_dir: Path, graph_name: str, cluster_count: int, *, add_self_loops: bool, symmetric: bool) -> dict[str, Any]:
    edges = pd.read_csv(output_dir / f"cluster_graph_{graph_name}_edges.csv")
    required = {"cluster_id_a", "cluster_id_b", "cluster_index_a", "cluster_index_b", "weight"}
    if not required.issubset(edges.columns):
        raise ValueError(f"{graph_name} edge schema is missing {sorted(required - set(edges.columns))}")
    for column in ["cluster_index_a", "cluster_index_b"]:
        if not edges.empty and not edges[column].astype(int).between(0, cluster_count - 1).all():
            raise ValueError(f"{graph_name} contains an invalid cluster index")
    raw = sparse.load_npz(output_dir / f"cluster_graph_{graph_name}_adjacency_raw.npz").tocsr()
    normalized = sparse.load_npz(output_dir / f"cluster_graph_{graph_name}_adjacency_normalized.npz").tocsr()
    if raw.shape != (cluster_count, cluster_count) or normalized.shape != raw.shape:
        raise ValueError(f"{graph_name} adjacency shape differs from cluster_index")
    if (raw != raw.T).nnz:
        raise ValueError(f"{graph_name} raw adjacency is not symmetric")
    work = raw.astype(float)
    if add_self_loops:
        work = work + sparse.eye(cluster_count, dtype=float, format="csr")
    degree = np.asarray(work.sum(axis=1)).ravel()
    if symmetric:
        scale = np.zeros_like(degree, dtype=float)
        scale[degree > 0] = 1.0 / np.sqrt(degree[degree > 0])
    else:
        scale = np.zeros_like(degree, dtype=float)
        scale[degree > 0] = 1.0 / degree[degree > 0]
    expected = sparse.diags(scale) @ work @ (sparse.diags(scale) if symmetric else sparse.eye(cluster_count))
    np.testing.assert_allclose(normalized.toarray(), expected.toarray())
    endpoints = set(edges.get("cluster_id_a", pd.Series(dtype=str)).astype(str)) | set(edges.get("cluster_id_b", pd.Series(dtype=str)).astype(str))
    return {"rows": len(edges), "endpoint_nodes": len(endpoints), "shape": raw.shape, "dtype": str(raw.dtype)}


def validate_metadata(path: Path, *, cluster_count: int, tensor_slots: int) -> dict[str, Any]:
    metadata = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "active_scope", "partition_gpkg", "road_relation_edges_csv", "output_root",
        "time_slot_minutes", "num_clusters", "num_segments", "order_stats",
        "service_type_counts", "data_min_departure_time", "data_max_departure_time",
        "data_min_slot_start", "data_max_slot_start", "num_tensor_slots",
        "poi_stats", "graph_summaries", "outputs", "config",
    }
    if set(metadata) != required:
        raise ValueError(f"metadata schema differs: missing={required - set(metadata)}, extra={set(metadata) - required}")
    if int(metadata["num_clusters"]) != cluster_count or int(metadata["num_tensor_slots"]) != tensor_slots:
        raise ValueError("metadata cluster or tensor-slot count differs")
    if set(metadata["service_type_counts"]) != SERVICE_TYPES:
        raise ValueError("metadata service-type schema differs")
    return metadata
