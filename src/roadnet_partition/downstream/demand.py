from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import contextlib
from copy import deepcopy
import os
from pathlib import Path
from typing import Any
import json
import shutil
import sqlite3
import time

try:
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None

from roadnet_partition.io import environment as _environment  # noqa: F401
import numpy as np
import pandas as pd

from roadnet_partition.config import (
    DEFAULT_GZIP_COMPRESSLEVEL,
    ResolvedStageConfig,
    config_fingerprint as compute_config_fingerprint,
    validate_gzip_compresslevel,
)
from roadnet_partition.downstream.order_checkpoints import (
    DriverBoundaryCheckpointWriter,
    LABELED_COLUMNS,
    LABELED_ORDER_CHECKPOINT,
    MATCHED_COLUMNS,
    MATCHED_ORDER_CHECKPOINT,
    ParquetCheckpointWriter,
    deterministic_shard_id,
    iter_checkpoint_batches,
    load_checkpoint_manifest,
    sorted_labeled_checkpoint_batches,
    sorted_labeled_od_batches,
    sorted_checkpoint_batches,
)
from roadnet_partition.io.manifests import collect_runtime_info, file_record, load_manifest
from roadnet_partition.graphs.build import (
    build_cluster_distance_graph, build_cluster_poi_graph, build_cluster_road_edges,
    save_graph_assets,
)
from roadnet_partition.io.geospatial import (
    PROJECT_ROOT, display_path, match_points_to_segments_with_distance, project_path,
)
from roadnet_partition.pipeline.results import RunContext, StageResult, StageStatus

EXCLUSIVE = "exclusive"
CARPOOL = "carpool"
SERVICE_TYPES = [EXCLUSIVE, CARPOOL]

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
    # Vectorized fill: map each OD row's (slot, origin, destination) to tensor
    # indices via the lookup dicts, drop unmapped keys (preserving the original
    # silent-filter semantics), and assign. Each (slot, o, d) key is unique per
    # the upstream SQL GROUP BY, so this last-writer-wins assignment is exactly
    # equivalent to the former itertuples loop.
    slot_idx = od_frame["slot_start"].astype(str).map(slot_to_index)
    origin_idx = od_frame["origin_cluster_id"].astype(str).map(cluster_to_index)
    dest_idx = od_frame["destination_cluster_id"].astype(str).map(cluster_to_index)
    mapped = slot_idx.notna() & origin_idx.notna() & dest_idx.notna()
    if bool(mapped.any()):
        s = slot_idx[mapped].to_numpy().astype(np.intp)
        o = origin_idx[mapped].to_numpy().astype(np.intp)
        d = dest_idx[mapped].to_numpy().astype(np.intp)
        tensors["Y_exclusive"][s, o, d] = od_frame.loc[mapped, "exclusive_count"].to_numpy().astype(np.int32)
        tensors["Y_carpool"][s, o, d] = od_frame.loc[mapped, "carpool_count"].to_numpy().astype(np.int32)
        tensors["Y_total"][s, o, d] = od_frame.loc[mapped, "total_count"].to_numpy().astype(np.int32)
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
    connection: sqlite3.Connection | None,
    config: dict[str, Any],
    partition,
    matched_writer: ParquetCheckpointWriter | None = None,
) -> dict[str, int]:
    if connection is None and matched_writer is None:
        raise ValueError("stage_order_assignments requires SQLite or a matched checkpoint writer")
    if connection is not None and matched_writer is not None:
        raise ValueError("stage_order_assignments accepts only one staging backend")
    pipeline = config["order_pipeline"]
    order_config = pipeline["order"]
    inputs = pipeline["inputs"]
    segment_to_cluster = dict(zip(partition["seg_id"].astype(str), partition["cluster_id"].astype(str)))
    segments = partition[["seg_id", "geometry"]].copy()

    from roadnet_partition.pipeline.timing import get_active_timer
    timer = get_active_timer()

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
    next_stage_id = 1

    for file_index, dataset in enumerate(inputs["order_datasets"]):
        order_path = Path(project_path(dataset))
        print(f"Reading orders from {order_path}...")
        reader = pd.read_csv(
            order_path,
            usecols=usecols,
            chunksize=chunksize,
            dtype={order_id_col: "string", driver_col: "string"},
        )
        chunk_index = 0
        while True:
            with timer.phase("csv_parse"):
                try:
                    chunk = next(reader)
                except StopIteration:
                    break
            chunk_index += 1
            timer.set_chunk(chunk_index, int(len(chunk)))
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
                work, pickup_lon, pickup_lat, segments, config["crs"]["geographic"], max_match_distance, tag="pickup",
            )
            dropoff_match = match_points_to_segments_with_distance(
                work, dropoff_lon, dropoff_lat, segments, config["crs"]["geographic"], max_match_distance, tag="dropoff",
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
            with timer.phase("segment_mapping"):
                origin_cluster = assigned_pickup.map(segment_to_cluster).astype(str).to_numpy()
                destination_cluster = assigned_dropoff.map(segment_to_cluster).astype(str).to_numpy()
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
                    "origin_cluster_id": origin_cluster,
                    "destination_cluster_id": destination_cluster,
                    "pickup_match_distance_m": pickup_match.loc[both_valid, "distance_m"].astype(float).to_numpy(),
                    "dropoff_match_distance_m": dropoff_match.loc[both_valid, "distance_m"].astype(float).to_numpy(),
                }
            )
            if matched_writer is not None:
                stage.insert(
                    0,
                    "stage_id",
                    np.arange(next_stage_id, next_stage_id + len(stage), dtype=np.int64),
                )
                with timer.phase("matched_parquet_write"):
                    matched_writer.write_frame(
                        stage.loc[:, MATCHED_COLUMNS],
                        deterministic_shard_id("matched", file_index, chunk_index),
                    )
                next_stage_id += len(stage)
            else:
                with timer.phase("sqlite_append"):
                    stage.to_sql("staged_orders", connection, if_exists="append", index=False)
            stats["staged_rows"] += int(len(stage))

            print(
                "order chunks: "
                f"file={file_index + 1}, chunk={chunk_index:,}, rows read={stats['rows_read']:,}, "
                f"window rows={stats['rows_after_time_window']:,}, staged rows={stats['staged_rows']:,}"
            )

    if connection is not None:
        with timer.phase("create_index_driver_time"):
            connection.execute("CREATE INDEX IF NOT EXISTS idx_staged_driver_time ON staged_orders(driver_id, departure_time_ns, finish_time_ns)")
        with timer.phase("create_index_slot_od"):
            connection.execute("CREATE INDEX IF NOT EXISTS idx_staged_slot_od ON staged_orders(slot_start_ns, origin_cluster_id, destination_cluster_id)")
        with timer.phase("sqlite_commit"):
            connection.commit()
    return stats

def label_staged_service_types(connection: sqlite3.Connection) -> dict[str, int]:
    from roadnet_partition.pipeline.timing import get_active_timer
    timer = get_active_timer()
    with timer.phase("service_labels_create_table"):
        connection.execute("DROP TABLE IF EXISTS service_labels")
        connection.execute("CREATE TABLE service_labels (stage_id INTEGER PRIMARY KEY, service_type TEXT NOT NULL)")
    counts = {EXCLUSIVE: 0, CARPOOL: 0}

    def emit(batch: list[tuple[int, str]]) -> None:
        with timer.phase("service_labels_insert"):
            connection.executemany("INSERT INTO service_labels(stage_id, service_type) VALUES (?, ?)", batch)
        for _, service_type in batch:
            counts[service_type] += 1

    with timer.phase("ordered_select_execute"):
        cursor = connection.execute(
            """
            SELECT stage_id, driver_id, departure_time_ns, finish_time_ns
            FROM staged_orders
            ORDER BY driver_id, departure_time_ns, finish_time_ns, stage_id
            """
        )
    with timer.phase("service_label_compute"):
        service_label_batches(cursor, emit)
    with timer.phase("service_labels_create_index"):
        connection.execute("CREATE INDEX IF NOT EXISTS idx_service_type ON service_labels(service_type)")
    with timer.phase("service_labels_commit"):
        connection.commit()
    return counts

def _iter_export_chunks(
    connection: sqlite3.Connection,
    query: str,
    chunksize: int,
    timer,
) -> Iterator[tuple[int, pd.DataFrame]]:
    if not timer.enabled:
        reader = pd.read_sql_query(query, connection, chunksize=chunksize)
        export_index = 1
        while True:
            with timer.phase("export_join_fetch"):
                try:
                    chunk = next(reader)
                except StopIteration:
                    return
            yield export_index, chunk
            export_index += 1
        return

    cursor = None
    try:
        with timer.phase("export_join_execute", chunk=0):
            cursor = connection.execute(query)
            columns = [description[0] for description in cursor.description]
        export_index = 1
        while True:
            with timer.phase("export_join_fetch", chunk=export_index):
                rows = cursor.fetchmany(chunksize)
            if not rows:
                return
            timer.set_chunk(export_index, len(rows), kind="export")
            with timer.phase("export_frame_build"):
                chunk = pd.DataFrame.from_records(rows, columns=columns, coerce_float=True)
            yield export_index, chunk
            export_index += 1
    finally:
        if cursor is not None:
            cursor.close()


def export_assigned_orders(
    connection: sqlite3.Connection,
    output_path: Path,
    chunksize: int = 100000,
    compresslevel: int = DEFAULT_GZIP_COMPRESSLEVEL,
) -> None:
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
    from roadnet_partition.pipeline.timing import get_active_timer, open_timed_gzip_text
    timer = get_active_timer()
    export_start = time.perf_counter() if timer.enabled else None
    first = True
    with open_timed_gzip_text(output_path, timer, compresslevel=compresslevel) as (handle, compressor):
        for export_index, chunk in _iter_export_chunks(connection, query, chunksize, timer):
            with timer.phase("export_datetime_format"):
                for source, target in [
                    ("departure_time_ns", "departure_time"),
                    ("finish_time_ns", "finish_time"),
                    ("slot_start_ns", "slot_start"),
                ]:
                    chunk[target] = pd.to_datetime(chunk[source], unit="ns").dt.strftime("%Y-%m-%d %H:%M:%S")
            with timer.phase("export_other_format"):
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
            if timer.enabled:
                before = compressor.write_seconds
                start = time.perf_counter()
                chunk.to_csv(handle, index=False, header=first)
                csv_wall = time.perf_counter() - start
                gzip_wall = compressor.write_seconds - before
                timer.record("export_csv_serialize", max(0.0, csv_wall - gzip_wall), chunk=export_index)
                timer.record("export_gzip_compress_write", gzip_wall, chunk=export_index)
            else:
                chunk.to_csv(handle, index=False, header=first)
            first = False
    if timer.enabled and export_start is not None:
        compressed_bytes = output_path.stat().st_size
        uncompressed_bytes = compressor.uncompressed_bytes
        timer.metric("export_wall_s", time.perf_counter() - export_start)
        timer.metric("export_uncompressed_bytes", uncompressed_bytes)
        timer.metric("export_compressed_bytes", compressed_bytes)
        timer.metric("export_compression_ratio", uncompressed_bytes / compressed_bytes if compressed_bytes else 0.0)

def build_cluster_od_from_staging(connection: sqlite3.Connection) -> pd.DataFrame:
    from roadnet_partition.pipeline.timing import get_active_timer
    timer = get_active_timer()
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
    with timer.phase("od_groupby_fetch"):
        grouped = pd.read_sql_query(query, connection)
    if grouped.empty:
        return aggregate_od_frame(pd.DataFrame(), 15)

    with timer.phase("od_pivot"):
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


PUBLIC_ASSIGNED_COLUMNS = [
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


@contextlib.contextmanager
def _columnar_phase(profile: dict[str, Any], name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        profile[name] = profile.get(name, 0.0) + (time.perf_counter() - started)


def _process_io() -> dict[str, int] | None:
    try:
        with open("/proc/self/io", encoding="utf-8") as handle:
            values = {}
            for line in handle:
                key, _, value = line.partition(":")
                values[key.strip()] = int(value)
            return {
                "read_bytes": int(values.get("read_bytes", 0)),
                "write_bytes": int(values.get("write_bytes", 0)),
            }
    except OSError:
        return None


def _peak_rss_gib() -> float | None:
    if resource is None:
        return None
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value / (1024.0 * 1024.0) if value else 0.0


def _process_usage() -> dict[str, float | int] | None:
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "user_s": float(usage.ru_utime),
        "sys_s": float(usage.ru_stime),
        "peak_rss_bytes": int(float(usage.ru_maxrss) * 1024),
    }


def _checkpoint_paths(manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent
    return [str((root / shard["path"]).resolve()) for shard in manifest["shards"]]


def _open_columnar_connection(
    manifest_path: Path,
    temp_directory: Path,
    settings: dict[str, Any],
):
    import duckdb

    connection = duckdb.connect(database=":memory:")
    connection.execute(f"SET memory_limit = '{settings['memory_limit']}'")
    connection.execute(f"SET threads = {int(settings['threads'])}")
    connection.execute("SET preserve_insertion_order = false")
    connection.execute("SET temp_directory = ?", [str(temp_directory.resolve())])
    return connection, _checkpoint_paths(manifest_path)


def _load_columnar_slot_bounds(
    manifest_path: Path,
    temp_directory: Path,
    settings: dict[str, Any],
) -> dict[str, int | None]:
    manifest = load_checkpoint_manifest(manifest_path, expected_kind=LABELED_ORDER_CHECKPOINT)
    if int(manifest["row_count"]) == 0:
        return {
            "min_departure_time_ns": None,
            "max_departure_time_ns": None,
            "min_slot_start_ns": None,
            "max_slot_start_ns": None,
        }
    connection, paths = _open_columnar_connection(manifest_path, temp_directory, settings)
    try:
        row = connection.from_query(
            """
            SELECT
                MIN(departure_time_ns),
                MAX(departure_time_ns),
                MIN(slot_start_ns),
                MAX(slot_start_ns)
            FROM read_parquet(?)
            """,
            params=[paths],
        ).fetchone()
    finally:
        connection.close()
    return {
        "min_departure_time_ns": None if row[0] is None else int(row[0]),
        "max_departure_time_ns": None if row[1] is None else int(row[1]),
        "min_slot_start_ns": None if row[2] is None else int(row[2]),
        "max_slot_start_ns": None if row[3] is None else int(row[3]),
    }


def _stream_cluster_od_to_csv(
    manifest_path: Path,
    *,
    temp_directory: Path,
    run_owned_root: Path,
    settings: dict[str, Any],
    output_path: Path,
    cluster_ids: list[str],
    slot_labels: np.ndarray,
    min_slot_start_ns: int | None,
    time_slot_minutes: int,
    profile: dict[str, Any],
    timer,
) -> dict[str, np.ndarray]:
    columns = [
        "slot_start",
        "origin_cluster_id",
        "destination_cluster_id",
        "exclusive_count",
        "carpool_count",
        "total_count",
    ]
    tensors = {
        "Y_exclusive": np.zeros((len(slot_labels), len(cluster_ids), len(cluster_ids)), dtype=np.int32),
        "Y_carpool": np.zeros((len(slot_labels), len(cluster_ids), len(cluster_ids)), dtype=np.int32),
        "Y_total": np.zeros((len(slot_labels), len(cluster_ids), len(cluster_ids)), dtype=np.int32),
        "slot_start": slot_labels,
        "cluster_ids": np.array(cluster_ids, dtype=str),
    }
    cluster_to_index = {cluster_id: index for index, cluster_id in enumerate(cluster_ids)}
    slot_step_ns = int(time_slot_minutes) * 60 * 1_000_000_000
    pending: list[tuple[int, str, str, int, int]] = []
    first = True
    group_count = 0

    def flush_pending(handle) -> None:
        nonlocal pending, first
        if not pending:
            return
        frame = pd.DataFrame(
            pending,
            columns=[
                "slot_start_ns",
                "origin_cluster_id",
                "destination_cluster_id",
                "exclusive_count",
                "carpool_count",
            ],
        )
        with _columnar_phase(profile, "od_csv"):
            with timer.phase("od_csv"):
                frame["slot_start"] = pd.to_datetime(frame["slot_start_ns"], unit="ns").dt.strftime("%Y-%m-%d %H:%M:%S")
                frame["total_count"] = frame["exclusive_count"] + frame["carpool_count"]
                frame.loc[:, columns].to_csv(handle, index=False, header=first)
        if min_slot_start_ns is not None:
            slot_index = ((frame["slot_start_ns"].to_numpy(dtype=np.int64) - min_slot_start_ns) // slot_step_ns)
            origin_index = frame["origin_cluster_id"].astype(str).map(cluster_to_index)
            destination_index = frame["destination_cluster_id"].astype(str).map(cluster_to_index)
            valid = (
                (slot_index >= 0)
                & (slot_index < len(slot_labels))
                & origin_index.notna().to_numpy()
                & destination_index.notna().to_numpy()
            )
            if bool(valid.any()):
                slots = slot_index[valid].astype(np.intp)
                origins = origin_index.to_numpy()[valid].astype(np.intp)
                destinations = destination_index.to_numpy()[valid].astype(np.intp)
                exclusive = frame["exclusive_count"].to_numpy(dtype=np.int32)[valid]
                carpool = frame["carpool_count"].to_numpy(dtype=np.int32)[valid]
                tensors["Y_exclusive"][slots, origins, destinations] = exclusive
                tensors["Y_carpool"][slots, origins, destinations] = carpool
                tensors["Y_total"][slots, origins, destinations] = exclusive + carpool
        pending = []
        first = False

    current_key: tuple[int, str, str] | None = None
    current_exclusive = 0
    current_carpool = 0

    def emit_current() -> None:
        nonlocal current_key, current_exclusive, current_carpool, group_count
        if current_key is None:
            return
        pending.append((*current_key, current_exclusive, current_carpool))
        group_count += 1
        current_key = None
        current_exclusive = 0
        current_carpool = 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        with sorted_labeled_od_batches(
            manifest_path,
            temp_directory=temp_directory,
            run_owned_root=run_owned_root,
            memory_limit=settings["memory_limit"],
            threads=int(settings["threads"]),
            batch_size=int(settings["batch_size"]),
        ) as (batches, sort_metrics):
            for batch_index, batch in enumerate(batches, start=1):
                with _columnar_phase(profile, "od_arrow_to_pandas"):
                    with timer.phase("od_arrow_to_pandas", chunk=batch_index):
                        frame = batch.to_pandas()
                for slot_ns, origin, destination, service_type in frame.loc[
                    :, ["slot_start_ns", "origin_cluster_id", "destination_cluster_id", "service_type"]
                ].itertuples(index=False, name=None):
                    key = (int(slot_ns), str(origin), str(destination))
                    if current_key != key:
                        emit_current()
                        current_key = key
                    if service_type == EXCLUSIVE:
                        current_exclusive += 1
                    elif service_type == CARPOOL:
                        current_carpool += 1
                    else:
                        raise ValueError(f"unknown service type: {service_type!r}")
                if pending and len(pending) >= 100_000:
                    flush_pending(handle)
            emit_current()
            flush_pending(handle)
        profile["od_sort"] = sort_metrics
    if first:
        pd.DataFrame(columns=columns).to_csv(output_path, index=False)
    profile["od_group_count"] = group_count
    return tensors


def export_assigned_orders_from_labeled(
    batches: Iterable[Any],
    output_path: Path,
    *,
    timer,
    compresslevel: int,
) -> None:
    from roadnet_partition.pipeline.timing import open_timed_gzip_text

    first = True
    with open_timed_gzip_text(output_path, timer, compresslevel=compresslevel) as (handle, compressor):
        for batch_index, batch in enumerate(batches, start=1):
            with timer.phase("compatibility_arrow_to_pandas", chunk=batch_index):
                chunk = batch.to_pandas()
            with timer.phase("compatibility_datetime_format", chunk=batch_index):
                for source, target in [
                    ("departure_time_ns", "departure_time"),
                    ("finish_time_ns", "finish_time"),
                    ("slot_start_ns", "slot_start"),
                ]:
                    chunk[target] = pd.to_datetime(chunk[source], unit="ns").dt.strftime("%Y-%m-%d %H:%M:%S")
            with timer.phase("compatibility_other_format", chunk=batch_index):
                chunk = chunk[PUBLIC_ASSIGNED_COLUMNS]
            if timer.enabled:
                before = compressor.write_seconds
                started = time.perf_counter()
                chunk.to_csv(handle, index=False, header=first)
                csv_wall = time.perf_counter() - started
                gzip_wall = compressor.write_seconds - before
                timer.record("compatibility_csv_serialize", max(0.0, csv_wall - gzip_wall), chunk=batch_index)
                timer.record("compatibility_gzip_compress_write", gzip_wall, chunk=batch_index)
            else:
                chunk.to_csv(handle, index=False, header=first)
            first = False


def _run_columnar_from_config(
    config: dict[str, Any],
    *,
    source_fingerprint: str | None = None,
    config_fingerprint_value: str | None = None,
) -> None:
    import duckdb

    from roadnet_partition.pipeline.timing import get_active_timer, reset as reset_timer

    timer = reset_timer("demand")
    started = time.perf_counter()
    process_io_start = _process_io()
    process_usage_start = _process_usage()
    profile: dict[str, Any] = {
        "backend": "parquet_duckdb_v2",
        "duckdb_version": duckdb.__version__,
        "sort_key": ["driver_id", "departure_time_ns", "finish_time_ns", "stage_id"],
        "memory_limit": config["order_staging"]["memory_limit"],
        "threads": int(config["order_staging"]["threads"]),
        "batch_size": int(config["order_staging"]["batch_size"]),
        "target_shard_rows": int(config["order_staging"]["target_shard_rows"]),
        "temp_disk_budget_bytes": int(config["order_staging"]["temp_disk_budget_bytes"]),
        "compatibility_export": bool(config["order_staging"]["compatibility_export"]),
    }
    output_dir = resolve_output_root(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    pipeline = config["order_pipeline"]
    gzip_compresslevel = validate_gzip_compresslevel(
        config.get("gzip_compresslevel", DEFAULT_GZIP_COMPRESSLEVEL),
    )
    partition_path = Path(project_path(pipeline["inputs"]["partition_gpkg"]))
    configured_relation_edges = pipeline["inputs"].get("road_relation_edges_csv")
    relation_edges_path = (
        Path(project_path(configured_relation_edges))
        if configured_relation_edges
        else default_relation_edges_path(config)
    )

    print(f"Loading partition from {partition_path}...")
    partition = load_partition(partition_path, config["crs"]["projected"])
    cluster_index = build_cluster_index(partition, config, output_dir)
    cluster_ids = cluster_index.sort_values("cluster_index")["cluster_id"].astype(str).tolist()
    cluster_to_index = dict(zip(cluster_ids, range(len(cluster_ids))))
    segment_to_cluster = dict(zip(partition["seg_id"].astype(str), partition["cluster_id"].astype(str)))

    columnar_root = output_dir / "columnar"
    matched_root = columnar_root / "matched"
    labeled_root = columnar_root / "labeled"
    sort_temp = columnar_root / "duckdb-temp-sort"
    export_temp = columnar_root / "duckdb-temp-stage-id"
    runtime = collect_runtime_info()
    runtime["duckdb"] = duckdb.__version__
    source_fingerprint = source_fingerprint or compute_config_fingerprint({
        "orders": [str(path) for path in pipeline["inputs"]["order_datasets"]],
        "partition": str(partition_path),
        "relation_edges": str(relation_edges_path),
        "poi": str(pipeline["inputs"]["poi_path"]),
    })
    config_fingerprint_value = config_fingerprint_value or compute_config_fingerprint(config)

    matched_writer = ParquetCheckpointWriter(
        matched_root,
        kind=MATCHED_ORDER_CHECKPOINT,
        source_fingerprint=source_fingerprint,
        config_fingerprint=config_fingerprint_value,
        runtime=runtime,
        duckdb_version=duckdb.__version__,
        target_rows=int(config["order_staging"]["target_shard_rows"]),
        stage_id_validation="sequential",
    )
    with _columnar_phase(profile, "matched_parquet_write"):
        order_stats = stage_order_assignments(
            None,
            config,
            partition,
            matched_writer=matched_writer,
        )
        matched_manifest_path = matched_writer.finish()
    profile["matched_manifest"] = matched_manifest_path.relative_to(output_dir).as_posix()
    matched_manifest_fingerprint = file_record(matched_manifest_path)["sha256"]

    label_counts = {EXCLUSIVE: 0, CARPOOL: 0}
    labeled_writer = DriverBoundaryCheckpointWriter(
        labeled_root,
        kind=LABELED_ORDER_CHECKPOINT,
        source_fingerprint=source_fingerprint,
        config_fingerprint=config_fingerprint_value,
        runtime=runtime,
        duckdb_version=duckdb.__version__,
        target_rows=int(config["order_staging"]["target_shard_rows"]),
        source_manifest_fingerprint=matched_manifest_fingerprint,
    )
    current_driver: str | None = None
    current_parts: list[pd.DataFrame] = []

    def flush_driver() -> None:
        nonlocal current_driver, current_parts
        if not current_parts:
            return
        frame = current_parts[0] if len(current_parts) == 1 else pd.concat(current_parts, ignore_index=True)
        labels: list[tuple[int, str]] = []
        with _columnar_phase(profile, "service_label_compute"):
            service_label_batches(
                frame[["stage_id", "driver_id", "departure_time_ns", "finish_time_ns"]].itertuples(
                    index=False,
                    name=None,
                ),
                labels.extend,
            )
        if len(labels) != len(frame):
            raise ValueError(f"service label count differs for driver {current_driver!r}")
        frame = frame.copy()
        frame["service_type"] = [label for _, label in labels]
        for _, service_type in labels:
            label_counts[service_type] += 1
        with _columnar_phase(profile, "labeled_parquet_write"):
            labeled_writer.write_driver(frame.loc[:, LABELED_COLUMNS].reset_index(drop=True))
        current_parts = []
        current_driver = None

    with _columnar_phase(profile, "duckdb_external_sort_and_arrow_stream"):
        with sorted_checkpoint_batches(
            matched_manifest_path,
            temp_directory=sort_temp,
            run_owned_root=output_dir,
            memory_limit=config["order_staging"]["memory_limit"],
            threads=int(config["order_staging"]["threads"]),
            batch_size=int(config["order_staging"]["batch_size"]),
        ) as (batches, sort_metrics):
            for batch in batches:
                frame = batch.to_pandas()
                driver_values = frame["driver_id"].astype(str).to_numpy()
                boundaries = np.flatnonzero(driver_values[1:] != driver_values[:-1]) + 1
                starts = [0, *boundaries.tolist()]
                ends = [*boundaries.tolist(), len(frame)]
                for start, end in zip(starts, ends):
                    piece = frame.iloc[start:end]
                    driver = str(piece["driver_id"].iloc[0])
                    if current_driver is None:
                        current_driver = driver
                    elif driver != current_driver:
                        flush_driver()
                        current_driver = driver
                    current_parts.append(piece.reset_index(drop=True))
        profile["sort"] = sort_metrics
        if int(sort_metrics["temp_peak_disk_bytes"] or 0) > int(config["order_staging"]["temp_disk_budget_bytes"]):
            raise RuntimeError("DuckDB sort temporary disk budget exceeded")
    flush_driver()
    with _columnar_phase(profile, "labeled_checkpoint_finalize"):
        labeled_manifest_path = labeled_writer.finish()
    profile["labeled_manifest"] = labeled_manifest_path.relative_to(output_dir).as_posix()

    slot_suffix = f"{int(pipeline['time_slot_minutes'])}min"
    staged_time_bounds = _load_columnar_slot_bounds(labeled_manifest_path, sort_temp, config["order_staging"])
    tensor_slot_labels = build_slot_labels_from_bounds(
        staged_time_bounds["min_slot_start_ns"],
        staged_time_bounds["max_slot_start_ns"],
        int(pipeline["time_slot_minutes"]),
    )
    od_path = output_dir / f"cluster_od_{slot_suffix}.csv"
    with _columnar_phase(profile, "od_from_labeled_checkpoint"):
        tensors = _stream_cluster_od_to_csv(
            labeled_manifest_path,
            temp_directory=sort_temp,
            run_owned_root=output_dir,
            settings=config["order_staging"],
            output_path=od_path,
            cluster_ids=cluster_ids,
            slot_labels=tensor_slot_labels,
            min_slot_start_ns=staged_time_bounds["min_slot_start_ns"],
            time_slot_minutes=int(pipeline["time_slot_minutes"]),
            profile=profile,
            timer=timer,
        )
    with _columnar_phase(profile, "npz_write"):
        tensor_path = output_dir / f"od_tensor_{slot_suffix}.npz"
        np.savez_compressed(tensor_path, **tensors)

    assigned_path: Path | None = None
    if bool(config["order_staging"]["compatibility_export"]):
        assigned_path = output_dir / "orders_region_assigned.csv.gz"
        with _columnar_phase(profile, "stage_id_reorder_wall"):
            with sorted_labeled_checkpoint_batches(
                labeled_manifest_path,
                temp_directory=export_temp,
                run_owned_root=output_dir,
                memory_limit=config["order_staging"]["memory_limit"],
                threads=int(config["order_staging"]["threads"]),
                batch_size=int(config["order_staging"]["batch_size"]),
            ) as (export_batches, export_sort_metrics):
                with _columnar_phase(profile, "compatibility_csv_export"):
                    export_assigned_orders_from_labeled(
                        export_batches,
                        assigned_path,
                        timer=timer,
                        compresslevel=gzip_compresslevel,
                    )
                profile["stage_id_reorder"] = export_sort_metrics
                if int(export_sort_metrics["temp_peak_disk_bytes"] or 0) > int(config["order_staging"]["temp_disk_budget_bytes"]):
                    raise RuntimeError("DuckDB stage_id reorder temporary disk budget exceeded")

    with _columnar_phase(profile, "other_writes"):
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
        poi_edges, poi_stats = build_cluster_poi_graph(
            partition,
            cluster_ids,
            cluster_to_index,
            config,
            output_dir,
        )
        graph_summaries.append(
            save_graph_assets("poi", poi_edges, len(cluster_ids), output_dir, pipeline.get("graph_normalization", {}))
        )
        distance_edges = build_cluster_distance_graph(cluster_index, cluster_ids, config)
        graph_summaries.append(
            save_graph_assets("distance", distance_edges, len(cluster_ids), output_dir, pipeline.get("graph_normalization", {}))
        )

    metadata_outputs = {
        "cluster_od": display_path(od_path),
        "od_tensor": display_path(tensor_path),
        "cluster_index": display_path(output_dir / "cluster_index.csv"),
        "metadata": display_path(output_dir / "metadata.json"),
    }
    if assigned_path is not None:
        metadata_outputs["orders_region_assigned"] = display_path(assigned_path)
    metadata = {
        "active_scope": active_scope_name(config),
        "partition_gpkg": display_path(partition_path),
        "road_relation_edges_csv": display_path(relation_edges_path),
        "output_root": display_path(output_dir),
        "time_slot_minutes": int(pipeline["time_slot_minutes"]),
        "num_clusters": int(len(cluster_ids)),
        "num_segments": int(len(partition)),
        "order_stats": order_stats,
        "service_type_counts": label_counts,
        "data_min_departure_time": format_timestamp_ns(staged_time_bounds["min_departure_time_ns"]),
        "data_max_departure_time": format_timestamp_ns(staged_time_bounds["max_departure_time_ns"]),
        "data_min_slot_start": format_timestamp_ns(staged_time_bounds["min_slot_start_ns"]),
        "data_max_slot_start": format_timestamp_ns(staged_time_bounds["max_slot_start_ns"]),
        "num_tensor_slots": int(len(tensor_slot_labels)),
        "poi_stats": poi_stats,
        "graph_summaries": graph_summaries,
        "outputs": metadata_outputs,
        "config": pipeline,
    }
    metadata_path = output_dir / "metadata.json"
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(json_safe(metadata), handle, ensure_ascii=False, indent=2)

    profile["stage_wall_seconds"] = time.perf_counter() - started
    profile["process_io_start"] = process_io_start
    profile["process_io_end"] = _process_io()
    if process_io_start and profile["process_io_end"]:
        profile["process_bytes_read"] = profile["process_io_end"]["read_bytes"] - process_io_start["read_bytes"]
        profile["process_bytes_written"] = profile["process_io_end"]["write_bytes"] - process_io_start["write_bytes"]
    process_usage_end = _process_usage()
    if process_usage_start and process_usage_end:
        profile["user_s"] = process_usage_end["user_s"] - process_usage_start["user_s"]
        profile["sys_s"] = process_usage_end["sys_s"] - process_usage_start["sys_s"]
        profile["cpu_pct"] = 100.0 * (profile["user_s"] + profile["sys_s"]) / profile["stage_wall_seconds"]
        profile["peak_rss_bytes"] = process_usage_end["peak_rss_bytes"]
    profile["peak_rss_gib"] = _peak_rss_gib()
    profile["columnar_peak_disk_bytes"] = sum(
        path.stat().st_size for path in columnar_root.rglob("*") if path.is_file()
    )
    profile["checkpoint_bytes_before_cleanup"] = sum(
        path.stat().st_size
        for root in (matched_root, labeled_root)
        for path in root.rglob("*")
        if path.is_file()
    )
    profile["temp_peak_disk_bytes"] = max(
        int(profile.get("sort", {}).get("temp_peak_disk_bytes") or 0),
        int(profile.get("stage_id_reorder", {}).get("temp_peak_disk_bytes") or 0),
    )
    profile_path = output_dir / "columnar_profile.json"
    profile_path.write_text(json.dumps(json_safe(profile), indent=2), encoding="utf-8")
    timer.finalize_io()
    if timer.enabled:
        timing_profile = timer.to_profile()
        timing_profile["backend"] = "parquet_duckdb_v2"
        timing_profile["columnar"] = json_safe(profile)
        (output_dir / "timing_profile.json").write_text(
            json.dumps(timing_profile, indent=2),
            encoding="utf-8",
        )
    for path in (sort_temp, export_temp):
        if path.exists():
            shutil.rmtree(path)
    print(f"Saved columnar Demand outputs to {output_dir}")

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

def run_from_config(
    config: dict[str, Any],
    *,
    source_fingerprint: str | None = None,
    config_fingerprint_value: str | None = None,
) -> None:
    if "order_pipeline" not in config:
        raise ValueError("configuration must contain an order_pipeline section.")
    if config.get("order_staging_backend", "sqlite_v1") == "parquet_duckdb_v2":
        _run_columnar_from_config(
            config,
            source_fingerprint=source_fingerprint,
            config_fingerprint_value=config_fingerprint_value,
        )
        return

    from roadnet_partition.pipeline.timing import reset as _reset_timer
    timer = _reset_timer("demand")

    output_dir = resolve_output_root(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    pipeline = config["order_pipeline"]
    gzip_compresslevel = validate_gzip_compresslevel(
        config.get("gzip_compresslevel", DEFAULT_GZIP_COMPRESSLEVEL),
    )
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
        timer.record_sqlite_size(db_path)
        from roadnet_partition.pipeline.timing import collect_sqlite_evidence
        sqlite_evidence = collect_sqlite_evidence(connection)
        print("Inferring service types from driver time overlaps...")
        service_counts = label_staged_service_types(connection)

        assigned_path = output_dir / "orders_region_assigned.csv.gz"
        print(f"Exporting assigned orders to {assigned_path}...")
        export_assigned_orders(connection, assigned_path, compresslevel=gzip_compresslevel)

        slot_suffix = f"{int(pipeline['time_slot_minutes'])}min"
        od = build_cluster_od_from_staging(connection)
        with timer.phase("od_csv"):
            od_path = output_dir / f"cluster_od_{slot_suffix}.csv"
            od.to_csv(od_path, index=False)

        staged_time_bounds = load_staged_slot_bounds(connection)
        tensor_slot_labels = build_slot_labels_from_bounds(
            staged_time_bounds["min_slot_start_ns"],
            staged_time_bounds["max_slot_start_ns"],
            int(pipeline["time_slot_minutes"]),
        )
        with timer.phase("tensor_alloc_fill"):
            tensors = build_od_tensors(od, cluster_ids, tensor_slot_labels)
        with timer.phase("npz_write"):
            tensor_path = output_dir / f"od_tensor_{slot_suffix}.npz"
            np.savez_compressed(tensor_path, **tensors)
        timer.record_sqlite_size(db_path)
        completed = True
    finally:
        connection.close()
        if completed and not bool(pipeline.get("keep_staging_db", False)) and db_path.exists():
            db_path.unlink()

    print("Building cluster-level graph assets...")
    with timer.phase("other_writes"):
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

    timer.finalize_io()
    timer.report()
    if timer.enabled:
        profile = timer.to_profile()
        profile["sqlite_evidence"] = sqlite_evidence
        (output_dir / "timing_profile.json").write_text(json.dumps(profile, indent=2, default=str), encoding="utf-8")
    print(f"Saved order-region pipeline outputs to {output_dir}")
    print(f"Saved metadata to {metadata_path}")


def run_demand(
    config: ResolvedStageConfig,
    context: RunContext,
    *,
    source_fingerprint: str | None = None,
) -> StageResult:
    if context.stage_dir is None or context.stage_name != "demand":
        raise ValueError("run_demand requires context.for_stage('demand')")
    output_dir = context.stage_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    values = deepcopy(dict(config.values))
    if "order_pipeline" not in values:
        raise ValueError("configuration must contain an order_pipeline section")
    values["order_pipeline"] = deepcopy(dict(values["order_pipeline"]))
    values["order_pipeline"]["outputs"] = {
        **dict(values["order_pipeline"].get("outputs", {})),
        "root": str(output_dir),
    }
    if (
        values.get("order_staging_backend") == "parquet_duckdb_v2"
        and not bool(values.get("order_staging", {}).get("compatibility_export", True))
    ):
        raise ValueError("compatibility_export=false is experimental and cannot be a formal Demand stage output")
    source_fingerprint = source_fingerprint or values.get("_resolved", {}).get("stage_input_fingerprint")
    if values.get("order_staging_backend") == "parquet_duckdb_v2":
        run_from_config(
            values,
            source_fingerprint=source_fingerprint,
            config_fingerprint_value=config.fingerprint,
        )
    else:
        run_from_config(values)

    suffix = f"{int(values['order_pipeline']['time_slot_minutes'])}min"
    names = {
        "cluster_index": "cluster_index.csv",
        "orders_region_assigned": "orders_region_assigned.csv.gz",
        "cluster_od": f"cluster_od_{suffix}.csv",
        "od_tensor": f"od_tensor_{suffix}.npz",
        "metadata": "metadata.json",
        "road_graph_edges": "cluster_graph_road_edges.csv",
        "road_adjacency_raw": "cluster_graph_road_adjacency_raw.npz",
        "road_adjacency_normalized": "cluster_graph_road_adjacency_normalized.npz",
        "poi_graph_edges": "cluster_graph_poi_edges.csv",
        "poi_adjacency_raw": "cluster_graph_poi_adjacency_raw.npz",
        "poi_adjacency_normalized": "cluster_graph_poi_adjacency_normalized.npz",
        "distance_graph_edges": "cluster_graph_distance_edges.csv",
        "distance_adjacency_raw": "cluster_graph_distance_adjacency_raw.npz",
        "distance_adjacency_normalized": "cluster_graph_distance_adjacency_normalized.npz",
        "poi_features": "cluster_poi_features.csv",
        "poi_category_mapping": "cluster_poi_category_mapping.csv",
    }
    outputs = {name: output_dir / filename for name, filename in names.items() if (output_dir / filename).exists()}
    metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
    return StageResult(
        stage="demand",
        status=StageStatus.COMPLETE,
        outputs=outputs,
        metrics={
            "orders": int(metadata["order_stats"]["staged_rows"]),
            "exclusive": int(metadata["service_type_counts"][EXCLUSIVE]),
            "carpool": int(metadata["service_type_counts"][CARPOOL]),
            "clusters": int(metadata["num_clusters"]),
            "tensor_slots": int(metadata["num_tensor_slots"]),
        },
    )
