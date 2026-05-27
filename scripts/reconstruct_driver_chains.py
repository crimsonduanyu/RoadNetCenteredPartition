from __future__ import annotations

import argparse
import gzip
import json
import logging
from pathlib import Path
import shutil
import sys
from typing import Iterable

import pandas as pd


MAX_GAP_MINUTES = 60
CARPOOL_MERGE_GAP_S = 0
SLOT_DURATION_MIN = 15
OUTPUT_DIR = "data/processed/fifth_ring/supply/"
ORDERS_PATH = "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
DEMAND_DIR = "data/processed/fifth_ring/demand"
DEMAND_TABLE = "demand_table"
MERGE_WITH_DEMAND = False
EXECUTION_MODE = "daily"
IO_CHUNK_ROWS = 500_000

DATETIME_COLUMNS = ["departure_time", "finish_time", "slot_start"]
ORDER_USE_COLUMNS = [
    "order_id",
    "driver_id",
    "departure_time",
    "finish_time",
    "origin_cluster_id",
    "destination_cluster_id",
    "service_type",
]
TRIP_SEGMENT_COLUMNS = [
    "segment_id",
    "driver_id",
    "trip_start",
    "trip_end",
    "origin_cluster_id",
    "destination_cluster_id",
    "service_type",
    "order_ids",
]
DRIVER_CHAIN_COLUMNS = [
    "chain_id",
    "driver_id",
    "date_",
    "chain_seq",
    "chain_start",
    "chain_end",
    "segment_count",
    "segment_ids",
]
IDLE_WINDOW_COLUMNS = ["chain_id", "driver_id", "idle_start", "idle_end", "idle_cluster_id", "idle_duration_s"]
AVAILABLE_COLUMNS = ["slot_start", "cluster_id", "available_vehicles"]
IN_SERVICE_COLUMNS = ["slot_start", "origin_cluster_id", "destination_cluster_id", "vehicles_in_service"]
FLEET_COLUMNS = ["slot_start", "cluster_id", "fleet_lower_bound_cluster", "global_fleet_lower_bound"]
OUTPUT_TABLES = {
    "trip_segments": ("trip_segments.csv.gz", TRIP_SEGMENT_COLUMNS),
    "driver_chains": ("driver_chains.csv.gz", DRIVER_CHAIN_COLUMNS),
    "idle_windows": ("idle_windows.csv.gz", IDLE_WINDOW_COLUMNS),
    "supply_available_by_cluster": ("supply_available_by_cluster.csv.gz", AVAILABLE_COLUMNS),
    "supply_in_service_od": ("supply_in_service_od.csv.gz", IN_SERVICE_COLUMNS),
    "supply_fleet_lower_bound": ("supply_fleet_lower_bound.csv.gz", FLEET_COLUMNS),
}

LOGGER = logging.getLogger(__name__)


def load_orders(path: str | Path = ORDERS_PATH) -> pd.DataFrame:
    """Load the pre-filtered Fifth Ring order table with timezone-naive datetimes."""
    orders = pd.read_csv(path)
    for column in DATETIME_COLUMNS:
        if column in orders.columns:
            orders[column] = pd.to_datetime(orders[column], errors="coerce")
    return orders


def configure_file_logging(output_dir: str | Path) -> None:
    """Attach a run.log file handler after the output directory is known."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "run.log"
    root = logging.getLogger()
    if not any(isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path for handler in root.handlers):
        file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(file_handler)


def parse_optional_date(value: str | None) -> pd.Timestamp | None:
    """Parse an optional YYYY-MM-DD date argument to a normalized timestamp."""
    if value is None:
        return None
    return pd.Timestamp(value).normalize()


def scan_departure_dates(
    orders_path: str | Path,
    io_chunk_rows: int = IO_CHUNK_ROWS,
    start_date: str | None = None,
    end_date: str | None = None,
    sample_days: int | None = None,
) -> list[str]:
    """Scan the input file in chunks and return sorted departure dates to process."""
    start = parse_optional_date(start_date)
    end = parse_optional_date(end_date)
    dates: set[str] = set()
    rows_scanned = 0
    for chunk_index, chunk in enumerate(
        pd.read_csv(orders_path, usecols=["departure_time"], chunksize=io_chunk_rows)
    ):
        rows_scanned += len(chunk)
        departure = pd.to_datetime(chunk["departure_time"], errors="coerce")
        valid = departure.notna()
        if start is not None:
            valid &= departure >= start
        if end is not None:
            valid &= departure < end + pd.Timedelta(days=1)
        dates.update(departure.loc[valid].dt.strftime("%Y-%m-%d").dropna().unique().tolist())
        LOGGER.info("Scanned date chunk %d: rows=%d cumulative_rows=%d dates=%d", chunk_index, len(chunk), rows_scanned, len(dates))

    sorted_dates = sorted(dates)
    if sample_days is not None:
        sorted_dates = sorted_dates[:sample_days]
    LOGGER.info("Found %d departure dates to process: %s", len(sorted_dates), sorted_dates)
    return sorted_dates


def load_orders_for_day(
    orders_path: str | Path,
    day: str,
    io_chunk_rows: int = IO_CHUNK_ROWS,
) -> pd.DataFrame:
    """Load all orders whose departure_time falls on the given natural day."""
    day_start = pd.Timestamp(day)
    day_end = day_start + pd.Timedelta(days=1)
    chunks = []
    total_rows = 0
    selected_rows = 0
    for chunk_index, chunk in enumerate(
        pd.read_csv(orders_path, usecols=ORDER_USE_COLUMNS, chunksize=io_chunk_rows)
    ):
        total_rows += len(chunk)
        departure = pd.to_datetime(chunk["departure_time"], errors="coerce")
        mask = (departure >= day_start) & (departure < day_end)
        if mask.any():
            selected = chunk.loc[mask].copy()
            selected["departure_time"] = departure.loc[mask].to_numpy()
            selected["finish_time"] = pd.to_datetime(selected["finish_time"], errors="coerce")
            chunks.append(selected)
            selected_rows += len(selected)
        if chunk_index % 20 == 0:
            LOGGER.info("Loading %s chunk %d: cumulative_rows=%d selected_rows=%d", day, chunk_index, total_rows, selected_rows)

    if not chunks:
        LOGGER.warning("No orders found for %s after date scan.", day)
        return pd.DataFrame(columns=ORDER_USE_COLUMNS)
    day_orders = pd.concat(chunks, ignore_index=True)
    LOGGER.info("Loaded %d orders for departure date %s.", len(day_orders), day)
    return day_orders


def filter_valid_orders(orders: pd.DataFrame) -> pd.DataFrame:
    """Flag and remove rows whose trip interval is missing or non-positive."""
    required = [
        "order_id",
        "driver_id",
        "departure_time",
        "finish_time",
        "origin_cluster_id",
        "destination_cluster_id",
        "service_type",
    ]
    missing = [column for column in required if column not in orders.columns]
    if missing:
        raise ValueError(f"orders table is missing required columns: {missing}")

    invalid = (
        orders["departure_time"].isna()
        | orders["finish_time"].isna()
        | (orders["finish_time"] <= orders["departure_time"])
    )
    if invalid.any():
        LOGGER.warning("Skipping %d trips with finish_time <= departure_time or missing times.", int(invalid.sum()))
    return orders.loc[~invalid].copy()


def resolve_carpool_trip_groups(
    orders: pd.DataFrame,
    carpool_merge_gap_s: int = CARPOOL_MERGE_GAP_S,
) -> pd.DataFrame:
    """Merge overlapping carpool order intervals into driver-level trip groups."""
    carpool = orders.loc[orders["service_type"].eq("carpool")].copy()
    if carpool.empty:
        return pd.DataFrame(columns=TRIP_SEGMENT_COLUMNS)

    carpool = carpool.sort_values(
        ["driver_id", "departure_time", "finish_time", "order_id"],
        kind="mergesort",
    ).reset_index(drop=True)
    running_end = carpool.groupby("driver_id")["finish_time"].cummax()
    prev_running_end = running_end.groupby(carpool["driver_id"]).shift()
    gap_s = (carpool["departure_time"] - prev_running_end).dt.total_seconds()
    if carpool_merge_gap_s == 0:
        new_group = prev_running_end.isna() | (gap_s >= 0.0)
    else:
        new_group = prev_running_end.isna() | (gap_s > float(carpool_merge_gap_s))
    carpool["group_num"] = new_group.groupby(carpool["driver_id"]).cumsum().astype("int64")

    group_keys = ["driver_id", "group_num"]
    grouped = carpool.groupby(group_keys, sort=False)
    starts = grouped["departure_time"].idxmin()
    ends = grouped["finish_time"].idxmax()

    groups = grouped.agg(
        trip_start=("departure_time", "min"),
        trip_end=("finish_time", "max"),
        order_ids=("order_id", list),
    ).reset_index()
    start_clusters = carpool.loc[starts, group_keys + ["origin_cluster_id"]].rename(
        columns={"origin_cluster_id": "origin_cluster_id"}
    )
    end_clusters = carpool.loc[ends, group_keys + ["destination_cluster_id"]].rename(
        columns={"destination_cluster_id": "destination_cluster_id"}
    )
    groups = groups.merge(start_clusters, on=group_keys, how="left")
    groups = groups.merge(end_clusters, on=group_keys, how="left")
    groups["service_type"] = "carpool"
    groups["segment_id"] = (
        "carpool_"
        + groups["driver_id"].astype(str)
        + "_"
        + groups["group_num"].astype(str)
    )
    groups = groups[TRIP_SEGMENT_COLUMNS]

    suspicious = (groups["trip_end"] - groups["trip_start"]) > pd.Timedelta(hours=4)
    if suspicious.any():
        LOGGER.warning("Flagged %d carpool groups longer than 4 hours.", int(suspicious.sum()))
    return groups


def build_exclusive_trip_segments(orders: pd.DataFrame) -> pd.DataFrame:
    """Convert each exclusive order into a single trip segment."""
    exclusive = orders.loc[orders["service_type"].eq("exclusive")].copy()
    if exclusive.empty:
        return pd.DataFrame(columns=TRIP_SEGMENT_COLUMNS)

    exclusive = exclusive.assign(
        segment_id="order_" + exclusive["order_id"].astype(str),
        trip_start=exclusive["departure_time"],
        trip_end=exclusive["finish_time"],
        order_ids=exclusive["order_id"].map(lambda order_id: [order_id]),
    )
    return exclusive[TRIP_SEGMENT_COLUMNS]


def build_trip_segments(
    orders: pd.DataFrame,
    carpool_merge_gap_s: int = CARPOOL_MERGE_GAP_S,
) -> pd.DataFrame:
    """Build unified trip segments from exclusive orders and merged carpool groups."""
    valid_orders = filter_valid_orders(orders)
    carpool = resolve_carpool_trip_groups(valid_orders, carpool_merge_gap_s)
    exclusive = build_exclusive_trip_segments(valid_orders)
    segment_frames = [frame for frame in [exclusive, carpool] if not frame.empty]
    if not segment_frames:
        return pd.DataFrame(columns=TRIP_SEGMENT_COLUMNS)
    trip_segments = pd.concat(segment_frames, ignore_index=True)
    return trip_segments.sort_values(
        ["driver_id", "trip_start", "trip_end", "segment_id"],
        kind="mergesort",
    ).reset_index(drop=True)


def reconstruct_driver_chains(
    trip_segments: pd.DataFrame,
    max_gap_minutes: int = MAX_GAP_MINUTES,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Assign driver-day chain identifiers using vectorized shifts and cumulative sums."""
    if trip_segments.empty:
        chains = pd.DataFrame(
            columns=[
                "chain_id",
                "driver_id",
                "date_",
                "chain_seq",
                "chain_start",
                "chain_end",
                "segment_count",
                "segment_ids",
            ]
        )
        return trip_segments.assign(chain_id=pd.Series(dtype="object")), chains

    segments = trip_segments.sort_values(
        ["driver_id", "trip_start", "trip_end", "segment_id"],
        kind="mergesort",
    ).reset_index(drop=True)
    segments["date_"] = segments["trip_start"].dt.date.astype(str)
    prev_end = segments.groupby("driver_id")["trip_end"].shift()
    prev_date = segments.groupby("driver_id")["date_"].shift()
    gap_min = (segments["trip_start"] - prev_end).dt.total_seconds().div(60)
    new_chain = prev_end.isna() | (gap_min > max_gap_minutes) | segments["date_"].ne(prev_date)
    segments["chain_seq"] = new_chain.groupby([segments["driver_id"], segments["date_"]]).cumsum().astype("int64")
    segments["chain_id"] = (
        segments["driver_id"].astype(str)
        + "_"
        + segments["date_"].astype(str)
        + "_"
        + segments["chain_seq"].astype(str)
    )

    grouped = segments.groupby(["chain_id", "driver_id", "date_", "chain_seq"], sort=False)
    chains = grouped.agg(
        chain_start=("trip_start", "first"),
        chain_end=("trip_end", "last"),
        segment_count=("segment_id", "size"),
        segment_ids=("segment_id", list),
    ).reset_index()

    chain_counts = chains.groupby(["driver_id", "date_"], as_index=False)["chain_id"].nunique()
    anomalies = chain_counts.loc[chain_counts["chain_id"] > 5]
    if not anomalies.empty:
        LOGGER.warning("Drivers with >5 chains on the same day: %d driver-days.", len(anomalies))
    return segments, chains


def extract_idle_windows(
    chain_segments: pd.DataFrame,
    max_gap_minutes: int = MAX_GAP_MINUTES,
) -> pd.DataFrame:
    """Extract positive inter-trip idle windows inside reconstructed chains."""
    if chain_segments.empty:
        return pd.DataFrame(
            columns=["chain_id", "driver_id", "idle_start", "idle_end", "idle_cluster_id", "idle_duration_s"]
        )

    segments = chain_segments.sort_values(
        ["chain_id", "trip_start", "trip_end", "segment_id"],
        kind="mergesort",
    ).reset_index(drop=True)
    next_start = segments.groupby("chain_id")["trip_start"].shift(-1)
    next_chain = segments.groupby("chain_id")["chain_id"].shift(-1)
    idle = segments.loc[next_start.notna()].copy()
    idle["idle_start"] = idle["trip_end"]
    idle["idle_end"] = next_start.loc[idle.index].to_numpy()
    idle["idle_duration_s"] = (idle["idle_end"] - idle["idle_start"]).dt.total_seconds()

    negative = idle["idle_duration_s"] < 0
    if negative.any():
        raise AssertionError(f"Found {int(negative.sum())} idle windows with negative duration.")

    idle = idle.loc[
        (idle["chain_id"].eq(next_chain.loc[idle.index]))
        & (idle["idle_duration_s"] > 0)
        & (idle["idle_duration_s"] <= max_gap_minutes * 60)
    ].copy()
    idle["idle_cluster_id"] = idle["destination_cluster_id"]
    return idle[["chain_id", "driver_id", "idle_start", "idle_end", "idle_cluster_id", "idle_duration_s"]]


def generate_slots(
    trip_segments: pd.DataFrame,
    idle_windows: pd.DataFrame,
    slot_duration_min: int = SLOT_DURATION_MIN,
) -> pd.DataFrame:
    """Generate continuous half-open slots covering all observed trip and idle activity."""
    starts = []
    ends = []
    if not trip_segments.empty:
        starts.append(trip_segments["trip_start"].min())
        ends.append(trip_segments["trip_end"].max())
    if not idle_windows.empty:
        starts.append(idle_windows["idle_start"].min())
        ends.append(idle_windows["idle_end"].max())
    if not starts:
        return pd.DataFrame(columns=["slot_start", "slot_end"])

    frequency = f"{slot_duration_min}min"
    first = min(starts).floor(frequency)
    last = max(ends).ceil(frequency)
    slots = pd.DataFrame({"slot_start": pd.date_range(first, last, freq=frequency, inclusive="left")})
    slots["slot_end"] = slots["slot_start"] + pd.Timedelta(minutes=slot_duration_min)
    return slots


def build_cluster_universe(trip_segments: pd.DataFrame, idle_windows: pd.DataFrame) -> pd.Index:
    """Collect all clusters observed in trip endpoints or idle locations."""
    cluster_series = []
    for column in ["origin_cluster_id", "destination_cluster_id"]:
        if column in trip_segments.columns:
            cluster_series.append(trip_segments[column])
    if "idle_cluster_id" in idle_windows.columns:
        cluster_series.append(idle_windows["idle_cluster_id"])
    if not cluster_series:
        return pd.Index([], name="cluster_id")
    clusters = pd.concat(cluster_series, ignore_index=True).dropna().drop_duplicates()
    return pd.Index(clusters.sort_values(kind="mergesort"), name="cluster_id")


def complete_slot_cluster_grid(
    frame: pd.DataFrame,
    slots: pd.DataFrame,
    clusters: pd.Index,
    value_columns: Iterable[str],
) -> pd.DataFrame:
    """Reindex a cluster-level supply table to every observed slot-cluster pair."""
    value_columns = list(value_columns)
    columns = ["slot_start", "cluster_id", *value_columns]
    if slots.empty or len(clusters) == 0:
        return pd.DataFrame(columns=columns)

    index = pd.MultiIndex.from_product(
        [slots["slot_start"], clusters],
        names=["slot_start", "cluster_id"],
    )
    completed = frame.set_index(["slot_start", "cluster_id"]).reindex(index)
    for column in value_columns:
        completed[column] = pd.to_numeric(completed[column], errors="coerce").fillna(0).astype("int64")
    return completed.reset_index()[columns]


def attach_global_fleet_to_all_clusters(
    fleet: pd.DataFrame,
    slots: pd.DataFrame,
    clusters: pd.Index,
) -> pd.DataFrame:
    """Complete fleet lower-bound rows and repeat each slot's global active-driver count."""
    completed = complete_slot_cluster_grid(fleet, slots, clusters, ["fleet_lower_bound_cluster"])
    if completed.empty:
        return completed.assign(global_fleet_lower_bound=pd.Series(dtype="int64"))

    global_fleet = (
        fleet[["slot_start", "global_fleet_lower_bound"]]
        .drop_duplicates("slot_start")
        .set_index("slot_start")
        .reindex(slots["slot_start"])
        .fillna(0)
        .astype("int64")
        .reset_index()
    )
    return completed.merge(global_fleet, on="slot_start", how="left")


def _expand_interval_slots(
    frame: pd.DataFrame,
    start_col: str,
    end_col: str,
    slot_duration_min: int,
    columns: Iterable[str],
) -> pd.DataFrame:
    """Map intervals to every half-open slot they overlap without a row-wise Python loop."""
    output_columns = ["slot_start", *columns]
    if frame.empty:
        return pd.DataFrame(columns=output_columns)

    frequency = f"{slot_duration_min}min"
    intervals = frame.loc[frame[end_col] > frame[start_col], [start_col, end_col, *columns]].copy()
    if intervals.empty:
        return pd.DataFrame(columns=output_columns)

    intervals["_slot_start"] = intervals[start_col].dt.floor(frequency)
    intervals["_slot_end"] = (intervals[end_col] - pd.Timedelta(nanoseconds=1)).dt.floor(frequency)
    intervals["_slot_count"] = (
        (intervals["_slot_end"] - intervals["_slot_start"]).dt.total_seconds()
        // (slot_duration_min * 60)
        + 1
    ).astype("int64")
    intervals = intervals.loc[intervals["_slot_count"] > 0]
    repeated = intervals.loc[intervals.index.repeat(intervals["_slot_count"])].copy()
    repeated["_slot_offset"] = repeated.groupby(level=0).cumcount()
    repeated["slot_start"] = repeated["_slot_start"] + pd.to_timedelta(
        repeated["_slot_offset"] * slot_duration_min,
        unit="m",
    )
    return repeated[output_columns].reset_index(drop=True)


def compute_available_by_cluster(
    idle_windows: pd.DataFrame,
    slot_duration_min: int = SLOT_DURATION_MIN,
    return_driver_slots: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    """Count distinct idle drivers by slot and last-known cluster."""
    idle_driver_slots = _expand_interval_slots(
        idle_windows,
        "idle_start",
        "idle_end",
        slot_duration_min,
        ["driver_id", "idle_cluster_id"],
    ).rename(columns={"idle_cluster_id": "cluster_id"})
    if idle_driver_slots.empty:
        available = pd.DataFrame(columns=["slot_start", "cluster_id", "available_vehicles"])
    else:
        available = (
            idle_driver_slots.groupby(["slot_start", "cluster_id"], as_index=False)["driver_id"]
            .nunique()
            .rename(columns={"driver_id": "available_vehicles"})
        )
    if return_driver_slots:
        return available, idle_driver_slots
    return available


def compute_in_service_od(
    trip_segments: pd.DataFrame,
    slot_duration_min: int = SLOT_DURATION_MIN,
    return_driver_slots: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    """Count distinct in-service drivers per slot and trip OD pair."""
    trip_driver_slots = _expand_interval_slots(
        trip_segments,
        "trip_start",
        "trip_end",
        slot_duration_min,
        ["driver_id", "origin_cluster_id", "destination_cluster_id"],
    )
    if trip_driver_slots.empty:
        in_service = pd.DataFrame(
            columns=["slot_start", "origin_cluster_id", "destination_cluster_id", "vehicles_in_service"]
        )
    else:
        in_service = (
            trip_driver_slots.groupby(
                ["slot_start", "origin_cluster_id", "destination_cluster_id"],
                as_index=False,
            )["driver_id"]
            .nunique()
            .rename(columns={"driver_id": "vehicles_in_service"})
        )
    if return_driver_slots:
        return in_service, trip_driver_slots
    return in_service


def compute_fleet_lower_bound(
    idle_driver_slots: pd.DataFrame,
    trip_driver_slots: pd.DataFrame,
) -> pd.DataFrame:
    """Compute cluster and global distinct-driver lower bounds from idle and in-service activity."""
    idle_activity = idle_driver_slots[["slot_start", "driver_id", "cluster_id"]].copy()

    # A trip contributes the active driver to both endpoint clusters for the slot.
    origin_activity = trip_driver_slots[["slot_start", "driver_id", "origin_cluster_id"]].rename(
        columns={"origin_cluster_id": "cluster_id"}
    )
    dest_activity = trip_driver_slots[["slot_start", "driver_id", "destination_cluster_id"]].rename(
        columns={"destination_cluster_id": "cluster_id"}
    )
    activity_frames = [frame for frame in [idle_activity, origin_activity, dest_activity] if not frame.empty]
    if not activity_frames:
        return pd.DataFrame(columns=["slot_start", "cluster_id", "fleet_lower_bound_cluster", "global_fleet_lower_bound"])
    activity = pd.concat(activity_frames, ignore_index=True)

    activity = activity.dropna(subset=["slot_start", "driver_id", "cluster_id"])
    cluster = (
        activity.drop_duplicates(["slot_start", "cluster_id", "driver_id"])
        .groupby(["slot_start", "cluster_id"], as_index=False)["driver_id"]
        .nunique()
        .rename(columns={"driver_id": "fleet_lower_bound_cluster"})
    )
    global_activity = (
        activity.drop_duplicates(["slot_start", "driver_id"])
        .groupby("slot_start", as_index=False)["driver_id"]
        .nunique()
        .rename(columns={"driver_id": "global_fleet_lower_bound"})
    )
    return cluster.merge(global_activity, on="slot_start", how="left")


def compute_supply_variables(
    trip_segments: pd.DataFrame,
    idle_windows: pd.DataFrame,
    slot_duration_min: int = SLOT_DURATION_MIN,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Compute slot-level available, in-service OD, and fleet lower-bound tables."""
    available, idle_driver_slots = compute_available_by_cluster(idle_windows, slot_duration_min, True)
    in_service, trip_driver_slots = compute_in_service_od(trip_segments, slot_duration_min, True)
    fleet = compute_fleet_lower_bound(idle_driver_slots, trip_driver_slots)
    slots = generate_slots(trip_segments, idle_windows, slot_duration_min)
    clusters = build_cluster_universe(trip_segments, idle_windows)
    available = complete_slot_cluster_grid(available, slots, clusters, ["available_vehicles"])
    fleet = attach_global_fleet_to_all_clusters(fleet, slots, clusters)
    return available, in_service, fleet


def serialize_list_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Convert list-valued columns to JSON strings for CSV persistence."""
    output = frame.copy()
    for column in ["order_ids", "segment_ids"]:
        if column in output.columns:
            output[column] = output[column].map(json.dumps)
    return output


def save_csv_gz(frame: pd.DataFrame, path: str | Path) -> None:
    """Write a dataframe to a gzip-compressed CSV, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialize_list_columns(frame).to_csv(path, index=False, compression="gzip")
    LOGGER.info("Wrote %s (%d rows).", path, len(frame))


def process_orders_frame(
    orders: pd.DataFrame,
    max_gap_minutes: int = MAX_GAP_MINUTES,
    carpool_merge_gap_s: int = CARPOOL_MERGE_GAP_S,
    slot_duration_min: int = SLOT_DURATION_MIN,
) -> dict[str, pd.DataFrame]:
    """Process one in-memory order frame into all supply-side output tables."""
    trip_segments = build_trip_segments(orders, carpool_merge_gap_s)
    chain_segments, driver_chains = reconstruct_driver_chains(trip_segments, max_gap_minutes)
    idle_windows = extract_idle_windows(chain_segments, max_gap_minutes)
    available, in_service, fleet = compute_supply_variables(trip_segments, idle_windows, slot_duration_min)
    return {
        "trip_segments": trip_segments,
        "driver_chains": driver_chains,
        "idle_windows": idle_windows,
        "supply_available_by_cluster": available,
        "supply_in_service_od": in_service,
        "supply_fleet_lower_bound": fleet,
    }


def process_day_to_parts(
    orders_path: str | Path,
    day: str,
    daily_parts_dir: str | Path,
    max_gap_minutes: int = MAX_GAP_MINUTES,
    carpool_merge_gap_s: int = CARPOOL_MERGE_GAP_S,
    slot_duration_min: int = SLOT_DURATION_MIN,
    io_chunk_rows: int = IO_CHUNK_ROWS,
) -> dict[str, int | str]:
    """Load one natural day, process it independently, and write day-level part files."""
    day_orders = load_orders_for_day(orders_path, day, io_chunk_rows)
    outputs = process_orders_frame(day_orders, max_gap_minutes, carpool_merge_gap_s, slot_duration_min)
    day_dir = Path(daily_parts_dir) / f"date={day}"
    day_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, int | str] = {"date": day, "orders_loaded": len(day_orders)}
    valid_mask = (
        day_orders["departure_time"].notna()
        & day_orders["finish_time"].notna()
        & (day_orders["finish_time"] > day_orders["departure_time"])
    )
    summary["valid_orders"] = int(valid_mask.sum()) if not day_orders.empty else 0
    summary["invalid_orders"] = int((~valid_mask).sum()) if not day_orders.empty else 0

    for table_name, (filename, _) in OUTPUT_TABLES.items():
        frame = outputs[table_name]
        save_csv_gz(frame, day_dir / filename)
        summary[f"{table_name}_rows"] = len(frame)
    LOGGER.info("Completed %s summary: %s", day, summary)
    return summary


def write_empty_output(path: str | Path, columns: list[str]) -> None:
    """Write an empty gzip CSV with only the header row."""
    save_csv_gz(pd.DataFrame(columns=columns), path)


def merge_gzip_csv_parts(part_paths: list[Path], output_path: str | Path, columns: list[str]) -> int:
    """Merge gzip CSV part files by streaming text without loading them into memory."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not part_paths:
        write_empty_output(output_path, columns)
        return 0

    total_rows = 0
    with gzip.open(output_path, "wt", encoding="utf-8", newline="") as out_handle:
        wrote_header = False
        for part_path in sorted(part_paths):
            with gzip.open(part_path, "rt", encoding="utf-8", newline="") as in_handle:
                header = in_handle.readline()
                if header and not wrote_header:
                    out_handle.write(header)
                    wrote_header = True
                for line in in_handle:
                    out_handle.write(line)
                    total_rows += 1
        if not wrote_header:
            out_handle.write(",".join(columns) + "\n")
    LOGGER.info("Merged %d parts into %s (%d data rows).", len(part_paths), output_path, total_rows)
    return total_rows


def merge_daily_parts(daily_parts_dir: str | Path, output_dir: str | Path) -> dict[str, int]:
    """Merge all day-level part files into the final requested output files."""
    daily_parts_dir = Path(daily_parts_dir)
    output_dir = Path(output_dir)
    merged_rows = {}
    for table_name, (filename, columns) in OUTPUT_TABLES.items():
        part_paths = list(daily_parts_dir.glob(f"date=*/{filename}"))
        merged_rows[table_name] = merge_gzip_csv_parts(part_paths, output_dir / filename, columns)
    return merged_rows


def write_json(data: dict, path: str | Path) -> None:
    """Write JSON with a stable indentation for run metadata."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def merge_supply_with_demand(
    output_dir: str | Path,
    demand_table: str = DEMAND_TABLE,
    demand_dir: str | Path = DEMAND_DIR,
) -> pd.DataFrame:
    """Left-join cluster-level supply variables into a demand table."""
    output_dir = Path(output_dir)
    demand_path = Path(demand_dir) / f"{demand_table}.csv.gz"
    demand = pd.read_csv(demand_path)
    demand["slot_start"] = pd.to_datetime(demand["slot_start"], errors="coerce")
    for column in ["cluster_id"]:
        if column in demand.columns:
            demand[column] = demand[column].astype(str)

    available = pd.read_csv(output_dir / "supply_available_by_cluster.csv.gz")
    fleet = pd.read_csv(output_dir / "supply_fleet_lower_bound.csv.gz")
    available["slot_start"] = pd.to_datetime(available["slot_start"], errors="coerce")
    fleet["slot_start"] = pd.to_datetime(fleet["slot_start"], errors="coerce")
    available["cluster_id"] = available["cluster_id"].astype(str)
    fleet["cluster_id"] = fleet["cluster_id"].astype(str)

    merged = demand.merge(available, on=["slot_start", "cluster_id"], how="left")
    merged = merged.merge(fleet, on=["slot_start", "cluster_id"], how="left")
    save_csv_gz(merged, output_dir / "supply_demand_merged.csv.gz")
    return merged


def run_pipeline(
    orders_path: str | Path = ORDERS_PATH,
    output_dir: str | Path = OUTPUT_DIR,
    max_gap_minutes: int = MAX_GAP_MINUTES,
    carpool_merge_gap_s: int = CARPOOL_MERGE_GAP_S,
    slot_duration_min: int = SLOT_DURATION_MIN,
    merge_demand: bool = MERGE_WITH_DEMAND,
    demand_table: str = DEMAND_TABLE,
    execution_mode: str = EXECUTION_MODE,
    io_chunk_rows: int = IO_CHUNK_ROWS,
    start_date: str | None = None,
    end_date: str | None = None,
    keep_daily_parts: bool = False,
    sample_days: int | None = None,
) -> dict[str, object]:
    """Run the complete supply-side reconstruction pipeline and write all outputs."""
    output_dir = Path(output_dir)
    configure_file_logging(output_dir)
    config_used = {
        "orders_path": str(orders_path),
        "output_dir": str(output_dir),
        "max_gap_minutes": max_gap_minutes,
        "carpool_merge_gap_s": carpool_merge_gap_s,
        "slot_duration_min": slot_duration_min,
        "merge_demand": merge_demand,
        "demand_table": demand_table,
        "execution_mode": execution_mode,
        "io_chunk_rows": io_chunk_rows,
        "start_date": start_date,
        "end_date": end_date,
        "keep_daily_parts": keep_daily_parts,
        "sample_days": sample_days,
    }
    write_json(config_used, output_dir / "config_used.json")

    if execution_mode == "daily":
        return run_daily_pipeline(
            orders_path=orders_path,
            output_dir=output_dir,
            max_gap_minutes=max_gap_minutes,
            carpool_merge_gap_s=carpool_merge_gap_s,
            slot_duration_min=slot_duration_min,
            merge_demand=merge_demand,
            demand_table=demand_table,
            io_chunk_rows=io_chunk_rows,
            start_date=start_date,
            end_date=end_date,
            keep_daily_parts=keep_daily_parts,
            sample_days=sample_days,
        )
    if execution_mode != "full-memory":
        raise ValueError(f"Unsupported execution_mode={execution_mode!r}; expected 'daily' or 'full-memory'.")

    orders = load_orders(orders_path)
    LOGGER.info("Loaded %d filtered Fifth Ring orders from %s.", len(orders), orders_path)
    outputs = process_orders_frame(orders, max_gap_minutes, carpool_merge_gap_s, slot_duration_min)
    trip_segments = outputs["trip_segments"]
    driver_chains = outputs["driver_chains"]
    idle_windows = outputs["idle_windows"]
    available = outputs["supply_available_by_cluster"]
    in_service = outputs["supply_in_service_od"]
    fleet = outputs["supply_fleet_lower_bound"]
    save_csv_gz(trip_segments, output_dir / "trip_segments.csv.gz")
    save_csv_gz(driver_chains, output_dir / "driver_chains.csv.gz")
    save_csv_gz(idle_windows, output_dir / "idle_windows.csv.gz")
    save_csv_gz(available, output_dir / "supply_available_by_cluster.csv.gz")
    save_csv_gz(in_service, output_dir / "supply_in_service_od.csv.gz")
    save_csv_gz(fleet, output_dir / "supply_fleet_lower_bound.csv.gz")
    if merge_demand:
        merge_supply_with_demand(output_dir, demand_table)

    summary = {
        "orders_loaded": len(orders),
        "trip_segments": len(trip_segments),
        "driver_chains": len(driver_chains),
        "idle_windows": len(idle_windows),
        "available_rows": len(available),
        "in_service_rows": len(in_service),
        "fleet_rows": len(fleet),
    }
    LOGGER.info("Run summary: %s", summary)
    write_json(summary, output_dir / "run_summary.json")
    return summary


def run_daily_pipeline(
    orders_path: str | Path = ORDERS_PATH,
    output_dir: str | Path = OUTPUT_DIR,
    max_gap_minutes: int = MAX_GAP_MINUTES,
    carpool_merge_gap_s: int = CARPOOL_MERGE_GAP_S,
    slot_duration_min: int = SLOT_DURATION_MIN,
    merge_demand: bool = MERGE_WITH_DEMAND,
    demand_table: str = DEMAND_TABLE,
    io_chunk_rows: int = IO_CHUNK_ROWS,
    start_date: str | None = None,
    end_date: str | None = None,
    keep_daily_parts: bool = False,
    sample_days: int | None = None,
) -> dict[str, object]:
    """Run supply reconstruction independently for each departure natural day."""
    output_dir = Path(output_dir)
    daily_parts_dir = output_dir / "_daily_parts"
    shutil.rmtree(daily_parts_dir, ignore_errors=True)
    daily_parts_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Starting daily supply pipeline. daily_parts_dir=%s", daily_parts_dir)

    dates = scan_departure_dates(orders_path, io_chunk_rows, start_date, end_date, sample_days)
    day_summaries = []
    for day in dates:
        day_summary = process_day_to_parts(
            orders_path=orders_path,
            day=day,
            daily_parts_dir=daily_parts_dir,
            max_gap_minutes=max_gap_minutes,
            carpool_merge_gap_s=carpool_merge_gap_s,
            slot_duration_min=slot_duration_min,
            io_chunk_rows=io_chunk_rows,
        )
        day_summaries.append(day_summary)
        write_json({"days": day_summaries}, output_dir / "run_summary.partial.json")

    merged_rows = merge_daily_parts(daily_parts_dir, output_dir)
    if merge_demand:
        merge_supply_with_demand(output_dir, demand_table)

    summary = {
        "execution_mode": "daily",
        "days_processed": len(dates),
        "orders_loaded": int(sum(int(day["orders_loaded"]) for day in day_summaries)),
        "valid_orders": int(sum(int(day["valid_orders"]) for day in day_summaries)),
        "invalid_orders": int(sum(int(day["invalid_orders"]) for day in day_summaries)),
        "trip_segments": merged_rows["trip_segments"],
        "driver_chains": merged_rows["driver_chains"],
        "idle_windows": merged_rows["idle_windows"],
        "available_rows": merged_rows["supply_available_by_cluster"],
        "in_service_rows": merged_rows["supply_in_service_od"],
        "fleet_rows": merged_rows["supply_fleet_lower_bound"],
        "daily_summaries": day_summaries,
    }
    write_json(summary, output_dir / "run_summary.json")
    LOGGER.info("Daily run summary: %s", {k: v for k, v in summary.items() if k != "daily_summaries"})

    if not keep_daily_parts:
        shutil.rmtree(daily_parts_dir, ignore_errors=True)
        LOGGER.info("Removed daily part directory %s.", daily_parts_dir)
    return summary


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for supply reconstruction."""
    parser = argparse.ArgumentParser(description="Reconstruct Fifth Ring supply-side driver chains.")
    parser.add_argument("--orders-path", default=ORDERS_PATH, help="Filtered orders_region_assigned.csv.gz path.")
    parser.add_argument("--max-gap", type=int, default=MAX_GAP_MINUTES, help="Maximum chain idle gap in minutes.")
    parser.add_argument("--carpool-merge-gap-s", type=int, default=CARPOOL_MERGE_GAP_S, help="Carpool merge tolerance.")
    parser.add_argument("--slot-duration", type=int, default=SLOT_DURATION_MIN, help="Slot duration in minutes.")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="Directory for supply output files.")
    parser.add_argument("--merge-demand", action="store_true", help="Merge cluster supply into the demand table.")
    parser.add_argument("--demand-table", default=DEMAND_TABLE, help="Demand table basename under the demand directory.")
    parser.add_argument(
        "--execution-mode",
        choices=["daily", "full-memory"],
        default=EXECUTION_MODE,
        help="Execution mode. daily processes one departure date at a time.",
    )
    parser.add_argument("--io-chunk-rows", type=int, default=IO_CHUNK_ROWS, help="Rows per CSV chunk when scanning/loading each day.")
    parser.add_argument("--start-date", default=None, help="First departure date to process, inclusive, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=None, help="Last departure date to process, inclusive, YYYY-MM-DD.")
    parser.add_argument("--keep-daily-parts", action="store_true", help="Keep _daily_parts after final files are merged.")
    parser.add_argument("--sample-days", type=int, default=None, help="Process only the first N discovered dates.")
    return parser


def main(argv: list[str] | None = None) -> dict[str, int]:
    """Parse CLI arguments and run supply-side reconstruction."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    args = build_parser().parse_args(argv)
    return run_pipeline(
        orders_path=args.orders_path,
        output_dir=args.output_dir,
        max_gap_minutes=args.max_gap,
        carpool_merge_gap_s=args.carpool_merge_gap_s,
        slot_duration_min=args.slot_duration,
        merge_demand=args.merge_demand,
        demand_table=args.demand_table,
        execution_mode=args.execution_mode,
        io_chunk_rows=args.io_chunk_rows,
        start_date=args.start_date,
        end_date=args.end_date,
        keep_daily_parts=args.keep_daily_parts,
        sample_days=args.sample_days,
    )


if __name__ == "__main__":
    main()
