from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
import gzip
import json
import math
import sqlite3
import sys

import env_setup  # noqa: F401
import numpy as np
import pandas as pd
from scipy import sparse


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXCLUSIVE = "exclusive"
CARPOOL = "carpool"
SERVICE_TYPES = [EXCLUSIVE, CARPOOL]


def project_path(path_value: str | Path | None) -> Path | None:
    if path_value is None:
        return None
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def display_path(path_value: str | Path) -> str:
    path = Path(path_value)
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def load_project_config(config_path: str | Path | None = None) -> dict[str, Any]:
    import yaml

    path = project_path(config_path) if config_path else PROJECT_ROOT / "config.yaml"
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def active_scope_name(config: dict[str, Any]) -> str:
    study_area = config["study_area"]
    return str(study_area.get("active", study_area.get("boundary_source", "fifth_ring")))


def resolve_output_root(config: dict[str, Any]) -> Path:
    pipeline = config["order_pipeline"]
    configured = pipeline.get("outputs", {}).get("root")
    if configured:
        return Path(project_path(configured))
    return PROJECT_ROOT / "data" / "processed" / active_scope_name(config) / "order_pipeline"


def default_relation_edges_path(config: dict[str, Any]) -> Path:
    return PROJECT_ROOT / "data" / "processed" / active_scope_name(config) / "segment_relation_edges_road_poi_order.csv"


def sort_cluster_ids(cluster_ids: Iterable[Any]) -> list[str]:
    def key(value: Any) -> tuple[int, int | str]:
        text = str(value)
        try:
            return (0, int(text))
        except ValueError:
            return (1, text)

    return sorted({str(cluster_id) for cluster_id in cluster_ids}, key=key)


def floor_datetimes_to_slot(series: pd.Series, minutes: int) -> pd.Series:
    if minutes <= 0:
        raise ValueError("time_slot_minutes must be positive.")
    return to_datetime_ns(series).dt.floor(f"{int(minutes)}min")


def to_datetime_ns(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce")
    return pd.Series(parsed, index=series.index).astype("datetime64[ns]")


def to_epoch_ns(series: pd.Series) -> pd.Series:
    datetimes = to_datetime_ns(series)
    return datetimes.astype("int64")


def service_label_batches(
    records: Iterable[tuple[int, str, int, int]],
    emit: Callable[[list[tuple[int, str]]], None],
    batch_size: int = 10000,
) -> None:
    """Emit service labels for records sorted by driver, start, finish.

    Intervals are half-open: [start, finish). A boundary touch does not overlap.
    """

    label_batch: list[tuple[int, str]] = []
    current_driver: str | None = None
    component_ids: list[int] = []
    component_max_end: int | None = None
    component_has_overlap = False

    def flush_label_batch() -> None:
        nonlocal label_batch
        if label_batch:
            emit(label_batch)
            label_batch = []

    def close_component() -> None:
        nonlocal component_ids, component_max_end, component_has_overlap
        if not component_ids:
            return
        label = CARPOOL if component_has_overlap else EXCLUSIVE
        for stage_id in component_ids:
            label_batch.append((stage_id, label))
            if len(label_batch) >= batch_size:
                flush_label_batch()
        component_ids = []
        component_max_end = None
        component_has_overlap = False

    for stage_id, driver_id, start_ns, finish_ns in records:
        driver_key = str(driver_id)
        if current_driver is None or driver_key != current_driver:
            close_component()
            current_driver = driver_key
            component_ids = [int(stage_id)]
            component_max_end = int(finish_ns)
            component_has_overlap = False
            continue

        if component_max_end is not None and int(start_ns) < component_max_end:
            component_has_overlap = True
            component_ids.append(int(stage_id))
            component_max_end = max(component_max_end, int(finish_ns))
        else:
            close_component()
            component_ids = [int(stage_id)]
            component_max_end = int(finish_ns)
            component_has_overlap = False

    close_component()
    flush_label_batch()


def infer_service_labels(records: Iterable[tuple[int, str, int, int]]) -> dict[int, str]:
    labels: dict[int, str] = {}

    def emit(batch: list[tuple[int, str]]) -> None:
        labels.update(batch)

    service_label_batches(records, emit)
    return labels


def normalize_adjacency(matrix: sparse.csr_matrix, add_self_loops: bool = True, symmetric: bool = True) -> sparse.csr_matrix:
    work = matrix.astype(float).tocsr()
    if add_self_loops:
        work = work + sparse.eye(work.shape[0], dtype=float, format="csr")

    degree = np.asarray(work.sum(axis=1)).ravel()
    if symmetric:
        inv_sqrt = np.zeros_like(degree, dtype=float)
        positive = degree > 0
        inv_sqrt[positive] = 1.0 / np.sqrt(degree[positive])
        normalizer = sparse.diags(inv_sqrt, format="csr")
        return (normalizer @ work @ normalizer).tocsr()

    inv = np.zeros_like(degree, dtype=float)
    positive = degree > 0
    inv[positive] = 1.0 / degree[positive]
    return (sparse.diags(inv, format="csr") @ work).tocsr()


def edges_to_adjacency(edges: pd.DataFrame, cluster_count: int) -> sparse.csr_matrix:
    if edges.empty:
        return sparse.csr_matrix((cluster_count, cluster_count), dtype=float)

    rows = edges["cluster_index_a"].astype(int).to_numpy()
    cols = edges["cluster_index_b"].astype(int).to_numpy()
    weights = edges["weight"].astype(float).to_numpy()
    all_rows = np.concatenate([rows, cols])
    all_cols = np.concatenate([cols, rows])
    all_weights = np.concatenate([weights, weights])
    return sparse.coo_matrix((all_weights, (all_rows, all_cols)), shape=(cluster_count, cluster_count)).tocsr()


def save_graph_assets(
    graph_name: str,
    edges: pd.DataFrame,
    cluster_count: int,
    output_dir: Path,
    normalization_config: dict[str, Any],
) -> dict[str, Any]:
    edge_path = output_dir / f"cluster_graph_{graph_name}_edges.csv"
    raw_path = output_dir / f"cluster_graph_{graph_name}_adjacency_raw.npz"
    normalized_path = output_dir / f"cluster_graph_{graph_name}_adjacency_normalized.npz"

    edge_columns = ["cluster_id_a", "cluster_id_b", "cluster_index_a", "cluster_index_b", "weight"]
    for column in edge_columns:
        if column not in edges.columns:
            edges[column] = pd.Series(dtype=float if column == "weight" else object)
    edges = edges.loc[:, [*edge_columns, *[column for column in edges.columns if column not in edge_columns]]]
    edges.to_csv(edge_path, index=False)

    adjacency = edges_to_adjacency(edges, cluster_count)
    normalized = normalize_adjacency(
        adjacency,
        add_self_loops=bool(normalization_config.get("add_self_loops", True)),
        symmetric=bool(normalization_config.get("symmetric", True)),
    )
    sparse.save_npz(raw_path, adjacency)
    sparse.save_npz(normalized_path, normalized)
    return {
        "name": graph_name,
        "edges": int(len(edges)),
        "edge_path": display_path(edge_path),
        "raw_adjacency_path": display_path(raw_path),
        "normalized_adjacency_path": display_path(normalized_path),
    }


def build_cluster_road_edges(
    relation_edges: pd.DataFrame,
    segment_to_cluster: dict[str, str],
    cluster_to_index: dict[str, int],
    weight_column: str = "base_weight",
) -> pd.DataFrame:
    required = {"seg_id_a", "seg_id_b"}
    missing = required - set(relation_edges.columns)
    if missing:
        raise ValueError(f"Road relation edge file is missing columns: {sorted(missing)}")
    if weight_column not in relation_edges.columns:
        if "weight" not in relation_edges.columns:
            raise ValueError(f"Road relation edge file lacks both '{weight_column}' and 'weight'.")
        weight_column = "weight"

    frame = relation_edges[["seg_id_a", "seg_id_b", weight_column]].copy()
    frame["cluster_a"] = frame["seg_id_a"].astype(str).map(segment_to_cluster)
    frame["cluster_b"] = frame["seg_id_b"].astype(str).map(segment_to_cluster)
    frame = frame.dropna(subset=["cluster_a", "cluster_b"]).copy()
    frame = frame.loc[frame["cluster_a"] != frame["cluster_b"]].copy()
    if frame.empty:
        return empty_cluster_edge_frame()

    frame["cluster_index_a_raw"] = frame["cluster_a"].map(cluster_to_index).astype(int)
    frame["cluster_index_b_raw"] = frame["cluster_b"].map(cluster_to_index).astype(int)
    frame["cluster_index_a"] = frame[["cluster_index_a_raw", "cluster_index_b_raw"]].min(axis=1)
    frame["cluster_index_b"] = frame[["cluster_index_a_raw", "cluster_index_b_raw"]].max(axis=1)
    frame["weight_value"] = pd.to_numeric(frame[weight_column], errors="coerce").fillna(0.0).clip(lower=0.0)

    grouped = (
        frame.groupby(["cluster_index_a", "cluster_index_b"], as_index=False)
        .agg(weight=("weight_value", "sum"), num_segment_edges=("weight_value", "size"))
        .sort_values(["cluster_index_a", "cluster_index_b"])
        .reset_index(drop=True)
    )
    index_to_cluster = {index: cluster_id for cluster_id, index in cluster_to_index.items()}
    grouped["cluster_id_a"] = grouped["cluster_index_a"].map(index_to_cluster)
    grouped["cluster_id_b"] = grouped["cluster_index_b"].map(index_to_cluster)
    return grouped[["cluster_id_a", "cluster_id_b", "cluster_index_a", "cluster_index_b", "weight", "num_segment_edges"]]


def empty_cluster_edge_frame(extra_columns: list[str] | None = None) -> pd.DataFrame:
    columns = ["cluster_id_a", "cluster_id_b", "cluster_index_a", "cluster_index_b", "weight"]
    if extra_columns:
        columns.extend(extra_columns)
    return pd.DataFrame(columns=columns)


def build_similarity_topk_edges(similarity: np.ndarray, cluster_ids: list[str], top_k: int) -> pd.DataFrame:
    cluster_count = len(cluster_ids)
    if cluster_count <= 1 or top_k <= 0:
        return empty_cluster_edge_frame(["similarity"])

    edge_map: dict[tuple[int, int], float] = {}
    for index in range(cluster_count):
        scores = similarity[index].copy()
        scores[index] = -np.inf
        candidate_indices = np.argsort(scores)[::-1]
        selected = [int(candidate) for candidate in candidate_indices if scores[candidate] > 0][:top_k]
        for candidate in selected:
            left, right = sorted((index, candidate))
            edge_map[(left, right)] = max(edge_map.get((left, right), 0.0), float(scores[candidate]))

    rows = [
        {
            "cluster_id_a": cluster_ids[left],
            "cluster_id_b": cluster_ids[right],
            "cluster_index_a": left,
            "cluster_index_b": right,
            "weight": weight,
            "similarity": weight,
        }
        for (left, right), weight in sorted(edge_map.items())
    ]
    return pd.DataFrame(rows) if rows else empty_cluster_edge_frame(["similarity"])


def aggregate_od_frame(orders: pd.DataFrame, time_slot_minutes: int) -> pd.DataFrame:
    if orders.empty:
        return pd.DataFrame(
            columns=["slot_start", "origin_cluster_id", "destination_cluster_id", "exclusive_count", "carpool_count", "total_count"]
        )

    frame = orders.copy()
    if "slot_start" not in frame.columns:
        frame["slot_start"] = floor_datetimes_to_slot(pd.to_datetime(frame["departure_time_ns"], unit="ns"), time_slot_minutes)
    grouped = (
        frame.groupby(["slot_start", "origin_cluster_id", "destination_cluster_id", "service_type"], observed=True)
        .size()
        .rename("order_count")
        .reset_index()
    )
    pivot = (
        grouped.pivot_table(
            index=["slot_start", "origin_cluster_id", "destination_cluster_id"],
            columns="service_type",
            values="order_count",
            aggfunc="sum",
            fill_value=0,
        )
        .reset_index()
        .rename_axis(None, axis=1)
    )
    for service_type in SERVICE_TYPES:
        if service_type not in pivot.columns:
            pivot[service_type] = 0
    pivot = pivot.rename(columns={EXCLUSIVE: "exclusive_count", CARPOOL: "carpool_count"})
    pivot["total_count"] = pivot["exclusive_count"].astype(int) + pivot["carpool_count"].astype(int)
    pivot["slot_start"] = pd.to_datetime(pivot["slot_start"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    return pivot[
        ["slot_start", "origin_cluster_id", "destination_cluster_id", "exclusive_count", "carpool_count", "total_count"]
    ].sort_values(["slot_start", "origin_cluster_id", "destination_cluster_id"])


def build_slot_labels(start_time: pd.Timestamp, end_time: pd.Timestamp, time_slot_minutes: int) -> np.ndarray:
    if end_time <= start_time:
        return np.array([], dtype=str)
    slots = pd.date_range(start=start_time, end=end_time, freq=f"{int(time_slot_minutes)}min", inclusive="left")
    return np.array([slot.strftime("%Y-%m-%d %H:%M:%S") for slot in slots], dtype=str)


def build_slot_labels_from_bounds(min_slot_ns: int | None, max_slot_ns: int | None, time_slot_minutes: int) -> np.ndarray:
    if min_slot_ns is None or max_slot_ns is None:
        return np.array([], dtype=str)
    start = pd.to_datetime(int(min_slot_ns), unit="ns")
    end = pd.to_datetime(int(max_slot_ns), unit="ns") + pd.Timedelta(minutes=int(time_slot_minutes))
    return build_slot_labels(start, end, time_slot_minutes)


def build_od_tensors(
    od_frame: pd.DataFrame,
    cluster_ids: list[str],
    slot_labels: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    if slot_labels is None:
        slots = (
            sorted(pd.to_datetime(od_frame["slot_start"]).dropna().unique())
            if not od_frame.empty and "slot_start" in od_frame.columns
            else []
        )
        slot_labels = np.array([pd.Timestamp(slot).strftime("%Y-%m-%d %H:%M:%S") for slot in slots], dtype=str)
    cluster_labels = np.array(cluster_ids, dtype=str)
    shape = (len(slot_labels), len(cluster_ids), len(cluster_ids))
    tensors = {
        "Y_exclusive": np.zeros(shape, dtype=np.int32),
        "Y_carpool": np.zeros(shape, dtype=np.int32),
        "Y_total": np.zeros(shape, dtype=np.int32),
        "slot_start": slot_labels,
        "cluster_ids": cluster_labels,
    }
    if od_frame.empty:
        return tensors

    slot_to_index = {label: index for index, label in enumerate(slot_labels)}
    cluster_to_index = {cluster_id: index for index, cluster_id in enumerate(cluster_ids)}
    for row in od_frame.itertuples(index=False):
        slot_index = slot_to_index.get(str(row.slot_start))
        origin_index = cluster_to_index.get(str(row.origin_cluster_id))
        destination_index = cluster_to_index.get(str(row.destination_cluster_id))
        if slot_index is None or origin_index is None or destination_index is None:
            continue
        tensors["Y_exclusive"][slot_index, origin_index, destination_index] = int(row.exclusive_count)
        tensors["Y_carpool"][slot_index, origin_index, destination_index] = int(row.carpool_count)
        tensors["Y_total"][slot_index, origin_index, destination_index] = int(row.total_count)
    return tensors


def load_partition(partition_path: Path, projected_crs: str):
    import geopandas as gpd

    partition = gpd.read_file(partition_path)
    missing = {"seg_id", "cluster_id"} - set(partition.columns)
    if missing:
        raise ValueError(f"Partition file must contain seg_id and cluster_id columns: {partition_path}")
    if partition.crs is None:
        raise ValueError(f"Partition file has no CRS: {partition_path}")
    if str(partition.crs) != projected_crs:
        partition = partition.to_crs(projected_crs)
    partition = partition.copy()
    partition["seg_id"] = partition["seg_id"].astype(str)
    partition["cluster_id"] = partition["cluster_id"].astype(str)
    if "length" not in partition.columns:
        partition["length"] = partition.geometry.length.astype(float)
    return partition


def build_cluster_index(partition, config: dict[str, Any], output_dir: Path) -> pd.DataFrame:
    import geopandas as gpd

    cluster_ids = sort_cluster_ids(partition["cluster_id"].unique())
    rows = []
    centroids = []
    for cluster_index, cluster_id in enumerate(cluster_ids):
        group = partition.loc[partition["cluster_id"] == cluster_id]
        geometry = group.geometry.unary_union
        centroid = geometry.centroid
        centroids.append(centroid)
        rows.append(
            {
                "cluster_index": cluster_index,
                "cluster_id": cluster_id,
                "num_segments": int(len(group)),
                "total_length_m": float(pd.to_numeric(group["length"], errors="coerce").fillna(0.0).sum()),
                "centroid_x": float(centroid.x),
                "centroid_y": float(centroid.y),
            }
        )

    index = pd.DataFrame(rows)
    centroid_gdf = gpd.GeoDataFrame(index[["cluster_index"]].copy(), geometry=centroids, crs=partition.crs)
    centroid_geo = centroid_gdf.to_crs(config["crs"]["geographic"])
    index["centroid_lon"] = centroid_geo.geometry.x.to_numpy(dtype=float)
    index["centroid_lat"] = centroid_geo.geometry.y.to_numpy(dtype=float)
    index.to_csv(output_dir / "cluster_index.csv", index=False)
    return index


def match_points_to_segments_with_distance(
    frame: pd.DataFrame,
    lon_col: str,
    lat_col: str,
    segments,
    source_crs: str,
    max_distance_m: float,
) -> pd.DataFrame:
    import geopandas as gpd
    from shapely.geometry import Point

    result = pd.DataFrame(index=frame.index, data={"seg_id": pd.NA, "distance_m": np.nan})
    valid = frame[[lon_col, lat_col]].notna().all(axis=1)
    valid &= np.isfinite(frame[lon_col]) & np.isfinite(frame[lat_col])
    if not bool(valid.any()):
        return result

    points = gpd.GeoDataFrame(
        {"row_id": frame.index[valid]},
        geometry=[Point(xy) for xy in zip(frame.loc[valid, lon_col], frame.loc[valid, lat_col])],
        crs=source_crs,
    ).to_crs(segments.crs)
    joined = gpd.sjoin_nearest(
        points,
        segments[["seg_id", "geometry"]],
        how="left",
        max_distance=max_distance_m,
        distance_col="distance_m",
    )
    matched = joined.dropna(subset=["seg_id"]).drop_duplicates("row_id")
    if matched.empty:
        return result

    row_ids = matched["row_id"].to_numpy()
    result.loc[row_ids, "seg_id"] = matched["seg_id"].astype(str).to_numpy()
    result.loc[row_ids, "distance_m"] = matched["distance_m"].astype(float).to_numpy()
    return result


def create_staging_database(db_path: Path) -> sqlite3.Connection:
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE staged_orders (
            stage_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            source_row INTEGER,
            order_id TEXT,
            driver_id TEXT NOT NULL,
            departure_time_ns INTEGER NOT NULL,
            finish_time_ns INTEGER NOT NULL,
            slot_start_ns INTEGER NOT NULL,
            pickup_seg_id TEXT NOT NULL,
            dropoff_seg_id TEXT NOT NULL,
            origin_cluster_id TEXT NOT NULL,
            destination_cluster_id TEXT NOT NULL,
            pickup_match_distance_m REAL,
            dropoff_match_distance_m REAL
        )
        """
    )
    connection.commit()
    return connection


def stage_order_assignments(
    connection: sqlite3.Connection,
    config: dict[str, Any],
    partition,
) -> dict[str, int]:
    pipeline = config["order_pipeline"]
    order_config = pipeline["order"]
    inputs = pipeline["inputs"]
    segment_to_cluster = dict(zip(partition["seg_id"].astype(str), partition["cluster_id"].astype(str)))
    segments = partition[["seg_id", "geometry"]].copy()

    order_id_col = order_config["order_id_column"]
    driver_col = order_config["driver_id_column"]
    pickup_lon = order_config["pickup_lon_column"]
    pickup_lat = order_config["pickup_lat_column"]
    dropoff_lon = order_config["dropoff_lon_column"]
    dropoff_lat = order_config["dropoff_lat_column"]
    departure_col = order_config["departure_time_column"]
    finish_col = order_config["finish_time_column"]
    usecols = [order_id_col, driver_col, pickup_lon, pickup_lat, dropoff_lon, dropoff_lat, departure_col, finish_col]
    chunksize = int(order_config["chunksize"])
    max_match_distance = float(order_config["max_match_distance_m"])
    start_time = pd.Timestamp(order_config["start_time"])
    end_time = pd.Timestamp(order_config["end_time"])
    slot_minutes = int(pipeline["time_slot_minutes"])

    stats = {
        "rows_read": 0,
        "invalid_time_rows": 0,
        "rows_after_time_window": 0,
        "invalid_driver_rows": 0,
        "invalid_coordinate_rows": 0,
        "pickup_matched_rows": 0,
        "dropoff_matched_rows": 0,
        "both_matched_rows": 0,
        "staged_rows": 0,
    }

    for file_index, dataset in enumerate(inputs["order_datasets"]):
        order_path = Path(project_path(dataset))
        print(f"Reading orders from {order_path}...")
        for chunk_index, chunk in enumerate(
            pd.read_csv(
                order_path,
                usecols=usecols,
                chunksize=chunksize,
                dtype={order_id_col: "string", driver_col: "string"},
            ),
            start=1,
        ):
            stats["rows_read"] += int(len(chunk))
            chunk["source_row"] = chunk.index.astype("int64")
            departure = to_datetime_ns(chunk[departure_col])
            finish = to_datetime_ns(chunk[finish_col])
            valid_time = departure.notna() & finish.notna() & (finish > departure)
            stats["invalid_time_rows"] += int((~valid_time).sum())

            in_window = valid_time & (departure >= start_time) & (departure < end_time)
            stats["rows_after_time_window"] += int(in_window.sum())
            if not bool(in_window.any()):
                continue

            work = chunk.loc[in_window].copy()
            work["_departure"] = departure.loc[in_window]
            work["_finish"] = finish.loc[in_window]

            driver = work[driver_col].astype("string")
            valid_driver = driver.notna() & driver.str.strip().ne("")
            stats["invalid_driver_rows"] += int((~valid_driver).sum())

            for column in [pickup_lon, pickup_lat, dropoff_lon, dropoff_lat]:
                work[column] = pd.to_numeric(work[column], errors="coerce")
            valid_coordinates = work[[pickup_lon, pickup_lat, dropoff_lon, dropoff_lat]].notna().all(axis=1)
            for column in [pickup_lon, pickup_lat, dropoff_lon, dropoff_lat]:
                valid_coordinates &= np.isfinite(work[column])
            stats["invalid_coordinate_rows"] += int((~valid_coordinates).sum())

            work = work.loc[valid_driver & valid_coordinates].copy()
            if work.empty:
                continue

            pickup_match = match_points_to_segments_with_distance(
                work,
                pickup_lon,
                pickup_lat,
                segments,
                config["crs"]["geographic"],
                max_match_distance,
            )
            dropoff_match = match_points_to_segments_with_distance(
                work,
                dropoff_lon,
                dropoff_lat,
                segments,
                config["crs"]["geographic"],
                max_match_distance,
            )

            pickup_seg = pickup_match["seg_id"]
            dropoff_seg = dropoff_match["seg_id"]
            pickup_valid = pickup_seg.notna()
            dropoff_valid = dropoff_seg.notna()
            both_valid = pickup_valid & dropoff_valid
            stats["pickup_matched_rows"] += int(pickup_valid.sum())
            stats["dropoff_matched_rows"] += int(dropoff_valid.sum())
            stats["both_matched_rows"] += int(both_valid.sum())
            if not bool(both_valid.any()):
                continue

            assigned = work.loc[both_valid].copy()
            assigned_pickup = pickup_seg.loc[both_valid].astype(str)
            assigned_dropoff = dropoff_seg.loc[both_valid].astype(str)
            departure_ns = to_epoch_ns(assigned["_departure"])
            finish_ns = to_epoch_ns(assigned["_finish"])
            slot_start_ns = to_epoch_ns(floor_datetimes_to_slot(assigned["_departure"], slot_minutes))
            stage = pd.DataFrame(
                {
                    "source_file": str(order_path.relative_to(PROJECT_ROOT) if order_path.is_relative_to(PROJECT_ROOT) else order_path),
                    "source_row": assigned["source_row"].astype("int64"),
                    "order_id": assigned[order_id_col].astype("string"),
                    "driver_id": assigned[driver_col].astype("string").str.strip(),
                    "departure_time_ns": departure_ns,
                    "finish_time_ns": finish_ns,
                    "slot_start_ns": slot_start_ns,
                    "pickup_seg_id": assigned_pickup.to_numpy(),
                    "dropoff_seg_id": assigned_dropoff.to_numpy(),
                    "origin_cluster_id": assigned_pickup.map(segment_to_cluster).astype(str).to_numpy(),
                    "destination_cluster_id": assigned_dropoff.map(segment_to_cluster).astype(str).to_numpy(),
                    "pickup_match_distance_m": pickup_match.loc[both_valid, "distance_m"].astype(float).to_numpy(),
                    "dropoff_match_distance_m": dropoff_match.loc[both_valid, "distance_m"].astype(float).to_numpy(),
                }
            )
            stage.to_sql("staged_orders", connection, if_exists="append", index=False)
            stats["staged_rows"] += int(len(stage))

            print(
                "order chunks: "
                f"file={file_index + 1}, chunk={chunk_index:,}, rows read={stats['rows_read']:,}, "
                f"window rows={stats['rows_after_time_window']:,}, staged rows={stats['staged_rows']:,}"
            )

    connection.execute("CREATE INDEX IF NOT EXISTS idx_staged_driver_time ON staged_orders(driver_id, departure_time_ns, finish_time_ns)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_staged_slot_od ON staged_orders(slot_start_ns, origin_cluster_id, destination_cluster_id)")
    connection.commit()
    return stats


def label_staged_service_types(connection: sqlite3.Connection) -> dict[str, int]:
    connection.execute("DROP TABLE IF EXISTS service_labels")
    connection.execute("CREATE TABLE service_labels (stage_id INTEGER PRIMARY KEY, service_type TEXT NOT NULL)")
    counts = {EXCLUSIVE: 0, CARPOOL: 0}

    def emit(batch: list[tuple[int, str]]) -> None:
        connection.executemany("INSERT INTO service_labels(stage_id, service_type) VALUES (?, ?)", batch)
        for _, service_type in batch:
            counts[service_type] += 1

    cursor = connection.execute(
        """
        SELECT stage_id, driver_id, departure_time_ns, finish_time_ns
        FROM staged_orders
        ORDER BY driver_id, departure_time_ns, finish_time_ns, stage_id
        """
    )
    service_label_batches(cursor, emit)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_service_type ON service_labels(service_type)")
    connection.commit()
    return counts


def export_assigned_orders(connection: sqlite3.Connection, output_path: Path, chunksize: int = 100000) -> None:
    query = """
        SELECT
            o.stage_id,
            o.source_file,
            o.source_row,
            o.order_id,
            o.driver_id,
            o.departure_time_ns,
            o.finish_time_ns,
            o.slot_start_ns,
            o.pickup_seg_id,
            o.dropoff_seg_id,
            o.origin_cluster_id,
            o.destination_cluster_id,
            o.pickup_match_distance_m,
            o.dropoff_match_distance_m,
            l.service_type
        FROM staged_orders o
        JOIN service_labels l ON o.stage_id = l.stage_id
        ORDER BY o.stage_id
    """
    first = True
    with gzip.open(output_path, "wt", encoding="utf-8", newline="") as handle:
        for chunk in pd.read_sql_query(query, connection, chunksize=chunksize):
            for source, target in [
                ("departure_time_ns", "departure_time"),
                ("finish_time_ns", "finish_time"),
                ("slot_start_ns", "slot_start"),
            ]:
                chunk[target] = pd.to_datetime(chunk[source], unit="ns").dt.strftime("%Y-%m-%d %H:%M:%S")
            chunk = chunk[
                [
                    "stage_id",
                    "source_file",
                    "source_row",
                    "order_id",
                    "driver_id",
                    "departure_time",
                    "finish_time",
                    "slot_start",
                    "pickup_seg_id",
                    "dropoff_seg_id",
                    "origin_cluster_id",
                    "destination_cluster_id",
                    "pickup_match_distance_m",
                    "dropoff_match_distance_m",
                    "service_type",
                ]
            ]
            chunk.to_csv(handle, index=False, header=first)
            first = False


def build_cluster_od_from_staging(connection: sqlite3.Connection) -> pd.DataFrame:
    query = """
        SELECT
            o.slot_start_ns,
            o.origin_cluster_id,
            o.destination_cluster_id,
            l.service_type,
            COUNT(*) AS order_count
        FROM staged_orders o
        JOIN service_labels l ON o.stage_id = l.stage_id
        GROUP BY o.slot_start_ns, o.origin_cluster_id, o.destination_cluster_id, l.service_type
        ORDER BY o.slot_start_ns, o.origin_cluster_id, o.destination_cluster_id, l.service_type
    """
    grouped = pd.read_sql_query(query, connection)
    if grouped.empty:
        return aggregate_od_frame(pd.DataFrame(), 15)

    grouped["slot_start"] = pd.to_datetime(grouped["slot_start_ns"], unit="ns").dt.strftime("%Y-%m-%d %H:%M:%S")
    pivot = (
        grouped.pivot_table(
            index=["slot_start", "origin_cluster_id", "destination_cluster_id"],
            columns="service_type",
            values="order_count",
            aggfunc="sum",
            fill_value=0,
        )
        .reset_index()
        .rename_axis(None, axis=1)
    )
    for service_type in SERVICE_TYPES:
        if service_type not in pivot.columns:
            pivot[service_type] = 0
    pivot = pivot.rename(columns={EXCLUSIVE: "exclusive_count", CARPOOL: "carpool_count"})
    pivot["exclusive_count"] = pivot["exclusive_count"].astype(int)
    pivot["carpool_count"] = pivot["carpool_count"].astype(int)
    pivot["total_count"] = pivot["exclusive_count"] + pivot["carpool_count"]
    return pivot[
        ["slot_start", "origin_cluster_id", "destination_cluster_id", "exclusive_count", "carpool_count", "total_count"]
    ].sort_values(["slot_start", "origin_cluster_id", "destination_cluster_id"])


def load_staged_slot_bounds(connection: sqlite3.Connection) -> dict[str, int | None]:
    row = connection.execute(
        """
        SELECT
            MIN(departure_time_ns) AS min_departure_time_ns,
            MAX(departure_time_ns) AS max_departure_time_ns,
            MIN(slot_start_ns) AS min_slot_start_ns,
            MAX(slot_start_ns) AS max_slot_start_ns
        FROM staged_orders
        """
    ).fetchone()
    if row is None:
        return {
            "min_departure_time_ns": None,
            "max_departure_time_ns": None,
            "min_slot_start_ns": None,
            "max_slot_start_ns": None,
        }
    return {
        "min_departure_time_ns": None if row[0] is None else int(row[0]),
        "max_departure_time_ns": None if row[1] is None else int(row[1]),
        "min_slot_start_ns": None if row[2] is None else int(row[2]),
        "max_slot_start_ns": None if row[3] is None else int(row[3]),
    }


def format_timestamp_ns(timestamp_ns: int | None) -> str | None:
    if timestamp_ns is None:
        return None
    return pd.to_datetime(int(timestamp_ns), unit="ns").strftime("%Y-%m-%d %H:%M:%S")


def build_cluster_poi_graph(
    partition,
    cluster_ids: list[str],
    cluster_to_index: dict[str, int],
    config: dict[str, Any],
    output_dir: Path,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    poi_config = config["order_pipeline"]["poi"]
    poi_path = project_path(config["order_pipeline"]["inputs"].get("poi_path"))
    if poi_path is None:
        return empty_cluster_edge_frame(["similarity"]), {"enabled": False, "valid_poi_rows": 0, "matched_poi_rows": 0}
    if not poi_path.exists():
        raise FileNotFoundError(f"Configured POI file does not exist: {poi_path}")

    lon_col = poi_config["lon_column"]
    lat_col = poi_config["lat_column"]
    category_col = poi_config["category_column"]
    poi = pd.read_csv(poi_path, usecols=[lon_col, lat_col, category_col])
    for column in [lon_col, lat_col]:
        poi[column] = pd.to_numeric(poi[column], errors="coerce")
    poi = poi.dropna(subset=[lon_col, lat_col, category_col]).copy()
    valid = np.isfinite(poi[lon_col]) & np.isfinite(poi[lat_col])
    poi = poi.loc[valid].copy()
    if poi.empty:
        return empty_cluster_edge_frame(["similarity"]), {"enabled": True, "valid_poi_rows": 0, "matched_poi_rows": 0}

    match = match_points_to_segments_with_distance(
        poi,
        lon_col,
        lat_col,
        partition[["seg_id", "geometry"]],
        config["crs"]["geographic"],
        float(poi_config["max_match_distance_m"]),
    )
    matched = poi.loc[match["seg_id"].notna()].copy()
    matched["seg_id"] = match.loc[match["seg_id"].notna(), "seg_id"].astype(str).to_numpy()
    segment_to_cluster = dict(zip(partition["seg_id"].astype(str), partition["cluster_id"].astype(str)))
    matched["cluster_id"] = matched["seg_id"].map(segment_to_cluster)
    matched = matched.dropna(subset=["cluster_id"]).copy()

    categories = sorted(matched[category_col].astype(str).unique().tolist())
    if categories:
        counts = (
            matched.groupby(["cluster_id", category_col])
            .size()
            .unstack(fill_value=0)
            .reindex(index=cluster_ids, columns=categories, fill_value=0)
        )
    else:
        counts = pd.DataFrame(index=cluster_ids)

    feature_frame = counts.reset_index().rename(columns={"index": "cluster_id"})
    rename_map = {category: f"poi_cat_{index:02d}" for index, category in enumerate(categories)}
    feature_frame = feature_frame.rename(columns=rename_map)
    category_map = pd.DataFrame(
        [{"category_col": column, "poi_type": category} for category, column in rename_map.items()]
    )
    feature_frame["poi_total"] = counts.sum(axis=1).to_numpy(dtype=int) if not counts.empty else 0
    feature_frame.to_csv(output_dir / "cluster_poi_features.csv", index=False)
    category_map.to_csv(output_dir / "cluster_poi_category_mapping.csv", index=False)

    values = counts.to_numpy(dtype=float) if not counts.empty else np.zeros((len(cluster_ids), 0), dtype=float)
    norms = np.linalg.norm(values, axis=1)
    similarity = np.zeros((len(cluster_ids), len(cluster_ids)), dtype=float)
    positive = norms > 0
    if values.shape[1] > 0 and bool(positive.any()):
        normalized = np.divide(values, norms[:, None], out=np.zeros_like(values), where=norms[:, None] > 0)
        similarity = normalized @ normalized.T
    edges = build_similarity_topk_edges(similarity, cluster_ids, int(poi_config["similarity_top_k"]))
    return edges, {"enabled": True, "valid_poi_rows": int(len(poi)), "matched_poi_rows": int(len(matched))}


def build_cluster_distance_graph(cluster_index: pd.DataFrame, cluster_ids: list[str], config: dict[str, Any]) -> pd.DataFrame:
    distance_config = config["order_pipeline"]["distance_graph"]
    top_k = int(distance_config["top_k"])
    decay = float(distance_config["decay_distance_m"])
    if decay <= 0:
        raise ValueError("order_pipeline.distance_graph.decay_distance_m must be positive.")
    if len(cluster_ids) <= 1 or top_k <= 0:
        return empty_cluster_edge_frame(["distance_m"])

    coords = cluster_index.sort_values("cluster_index")[["centroid_x", "centroid_y"]].to_numpy(dtype=float)
    edge_map: dict[tuple[int, int], float] = {}
    distance_map: dict[tuple[int, int], float] = {}
    for index in range(len(cluster_ids)):
        delta = coords - coords[index]
        distances = np.sqrt((delta * delta).sum(axis=1))
        distances[index] = np.inf
        for candidate in np.argsort(distances)[:top_k]:
            distance = float(distances[candidate])
            if not math.isfinite(distance):
                continue
            left, right = sorted((index, int(candidate)))
            weight = float(math.exp(-distance / decay))
            edge_map[(left, right)] = max(edge_map.get((left, right), 0.0), weight)
            distance_map[(left, right)] = min(distance_map.get((left, right), float("inf")), distance)

    rows = [
        {
            "cluster_id_a": cluster_ids[left],
            "cluster_id_b": cluster_ids[right],
            "cluster_index_a": left,
            "cluster_index_b": right,
            "weight": edge_map[(left, right)],
            "distance_m": distance_map[(left, right)],
        }
        for left, right in sorted(edge_map)
    ]
    return pd.DataFrame(rows) if rows else empty_cluster_edge_frame(["distance_m"])


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    return value


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv[1:]
    config = load_project_config(argv[0] if argv else None)
    if "order_pipeline" not in config:
        raise ValueError("config.yaml must contain an order_pipeline section.")

    output_dir = resolve_output_root(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    pipeline = config["order_pipeline"]
    partition_path = Path(project_path(pipeline["inputs"]["partition_gpkg"]))
    configured_relation_edges = pipeline["inputs"].get("road_relation_edges_csv")
    relation_edges_path = Path(project_path(configured_relation_edges)) if configured_relation_edges else default_relation_edges_path(config)

    print(f"Loading partition from {partition_path}...")
    partition = load_partition(partition_path, config["crs"]["projected"])
    cluster_index = build_cluster_index(partition, config, output_dir)
    cluster_ids = cluster_index.sort_values("cluster_index")["cluster_id"].astype(str).tolist()
    cluster_to_index = dict(zip(cluster_ids, range(len(cluster_ids))))
    segment_to_cluster = dict(zip(partition["seg_id"].astype(str), partition["cluster_id"].astype(str)))

    db_path = output_dir / "orders_region_staging.sqlite"
    connection = create_staging_database(db_path)
    completed = False
    try:
        order_stats = stage_order_assignments(connection, config, partition)
        print("Inferring service types from driver time overlaps...")
        service_counts = label_staged_service_types(connection)

        assigned_path = output_dir / "orders_region_assigned.csv.gz"
        print(f"Exporting assigned orders to {assigned_path}...")
        export_assigned_orders(connection, assigned_path)

        slot_suffix = f"{int(pipeline['time_slot_minutes'])}min"
        od = build_cluster_od_from_staging(connection)
        od_path = output_dir / f"cluster_od_{slot_suffix}.csv"
        od.to_csv(od_path, index=False)

        staged_time_bounds = load_staged_slot_bounds(connection)
        tensor_slot_labels = build_slot_labels_from_bounds(
            staged_time_bounds["min_slot_start_ns"],
            staged_time_bounds["max_slot_start_ns"],
            int(pipeline["time_slot_minutes"]),
        )
        tensors = build_od_tensors(od, cluster_ids, tensor_slot_labels)
        tensor_path = output_dir / f"od_tensor_{slot_suffix}.npz"
        np.savez_compressed(tensor_path, **tensors)
        completed = True
    finally:
        connection.close()
        if completed and not bool(pipeline.get("keep_staging_db", False)) and db_path.exists():
            db_path.unlink()

    print("Building cluster-level graph assets...")
    relation_edges = pd.read_csv(relation_edges_path)
    road_edges = build_cluster_road_edges(
        relation_edges,
        segment_to_cluster,
        cluster_to_index,
        pipeline.get("road_graph", {}).get("weight_column", "base_weight"),
    )
    graph_summaries = [
        save_graph_assets("road", road_edges, len(cluster_ids), output_dir, pipeline.get("graph_normalization", {}))
    ]

    poi_edges, poi_stats = build_cluster_poi_graph(partition, cluster_ids, cluster_to_index, config, output_dir)
    graph_summaries.append(
        save_graph_assets("poi", poi_edges, len(cluster_ids), output_dir, pipeline.get("graph_normalization", {}))
    )

    distance_edges = build_cluster_distance_graph(cluster_index, cluster_ids, config)
    graph_summaries.append(
        save_graph_assets("distance", distance_edges, len(cluster_ids), output_dir, pipeline.get("graph_normalization", {}))
    )

    metadata = {
        "active_scope": active_scope_name(config),
        "partition_gpkg": display_path(partition_path),
        "road_relation_edges_csv": display_path(relation_edges_path),
        "output_root": display_path(output_dir),
        "time_slot_minutes": int(pipeline["time_slot_minutes"]),
        "num_clusters": int(len(cluster_ids)),
        "num_segments": int(len(partition)),
        "order_stats": order_stats,
        "service_type_counts": service_counts,
        "data_min_departure_time": format_timestamp_ns(staged_time_bounds["min_departure_time_ns"]),
        "data_max_departure_time": format_timestamp_ns(staged_time_bounds["max_departure_time_ns"]),
        "data_min_slot_start": format_timestamp_ns(staged_time_bounds["min_slot_start_ns"]),
        "data_max_slot_start": format_timestamp_ns(staged_time_bounds["max_slot_start_ns"]),
        "num_tensor_slots": int(len(tensor_slot_labels)),
        "poi_stats": poi_stats,
        "graph_summaries": graph_summaries,
        "outputs": {
            "orders_region_assigned": display_path(assigned_path),
            "cluster_od": display_path(od_path),
            "od_tensor": display_path(tensor_path),
            "cluster_index": display_path(output_dir / "cluster_index.csv"),
            "metadata": display_path(output_dir / "metadata.json"),
        },
        "config": pipeline,
    }
    metadata_path = output_dir / "metadata.json"
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(json_safe(metadata), handle, ensure_ascii=False, indent=2)

    print(f"Saved order-region pipeline outputs to {output_dir}")
    print(f"Saved metadata to {metadata_path}")


if __name__ == "__main__":
    main()
