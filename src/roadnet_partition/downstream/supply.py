from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
from pathlib import Path
import sys
from typing import Iterable

import numpy as np
import pandas as pd

from roadnet_partition.config import ResolvedStageConfig
from roadnet_partition.downstream.order_checkpoints import (
    identifier_csv_options,
    invalid_driver_identifiers,
    normalize_driver_identifiers,
    normalize_order_identifiers,
)
from roadnet_partition.io.paths import resolve_path
from roadnet_partition.pipeline.results import RunContext, StageResult, StageStatus


MAX_GAP_MINUTES = 60  # chain-formation gap: trips farther apart start a new driver chain
TAU_IDLE_MINUTES = 30  # idle-judgement cap: a gap shorter than this counts as online-idle (-> available)
CARPOOL_MERGE_GAP_S = 0
SLOT_DURATION_MIN = 15
OUTPUT_DIR = "data/processed/fifth_ring/supply/"
ORDERS_PATH = "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
DEMAND_DIR = "data/processed/fifth_ring/demand"
DEMAND_TABLE = "demand_table"
MERGE_WITH_DEMAND = False
DRIVER_BLOCKS = 8  # number of per-driver chunks for supply reconstruction

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
IN_SERVICE_COLUMNS = ["slot_start", "origin_cluster_id", "destination_cluster_id", "vehicles_in_service"]
FLEET_COLUMNS = ["slot_start", "cluster_id", "fleet_lower_bound_cluster", "global_fleet_lower_bound"]

LOGGER = logging.getLogger(__name__)


def load_orders(path: str | Path = ORDERS_PATH) -> pd.DataFrame:
    """Load the pre-filtered Fifth Ring order table with timezone-naive datetimes.

    Reads only the columns the reconstruction actually consumes
    (``ORDER_USE_COLUMNS``) with explicit dtypes, so the load never materializes
    the wide unused columns (``source_file``, ``*_seg_id``, ``slot_start``, match
    distances, ...). The two time columns are read as text and parsed with an
    explicit format to keep the parse-time peak low. This is an I/O-footprint
    change only; the resulting values are identical to before.
    """
    columns = list(pd.read_csv(path, nrows=0, keep_default_na=False).columns)
    missing = [column for column in ORDER_USE_COLUMNS if column not in columns]
    if missing:
        raise ValueError(f"Supply orders are missing required columns: {missing}")
    options = identifier_csv_options()
    options["dtype"].update({
        "origin_cluster_id": "string",
        "destination_cluster_id": "string",
        "service_type": "string",
        "departure_time": "string",
        "finish_time": "string",
    })
    orders = pd.read_csv(
        path,
        usecols=ORDER_USE_COLUMNS,
        **options,
    )
    orders["order_id"] = normalize_order_identifiers(orders["order_id"])
    orders["driver_id"] = normalize_driver_identifiers(orders["driver_id"])
    invalid_driver = invalid_driver_identifiers(orders["driver_id"])
    if invalid_driver.any():
        first = int(np.flatnonzero(invalid_driver.to_numpy())[0]) + 2
        raise ValueError(
            f"Supply driver_id contains {int(invalid_driver.sum())} null or blank value(s); first at CSV row {first}"
        )
    invalid_service = ~orders["service_type"].isin({"exclusive", "carpool"})
    if invalid_service.any():
        first = int(np.flatnonzero(invalid_service.to_numpy())[0]) + 2
        raise ValueError(
            f"Supply service_type contains {int(invalid_service.sum())} invalid value(s); first at CSV row {first}"
        )
    for column in ("origin_cluster_id", "destination_cluster_id"):
        numeric = pd.to_numeric(orders[column], errors="coerce")
        valid = numeric.notna() & np.isfinite(numeric) & numeric.eq(np.floor(numeric))
        valid &= numeric.between(np.iinfo(np.int32).min, np.iinfo(np.int32).max)
        if not valid.all():
            first = int(np.flatnonzero((~valid).to_numpy())[0]) + 2
            raise ValueError(f"Supply {column} contains an invalid integer; first at CSV row {first}")
        orders[column] = numeric.astype("int32")
    for column in ("departure_time", "finish_time"):
        orders[column] = pd.to_datetime(orders[column], format="%Y-%m-%d %H:%M:%S", errors="coerce")
    orders["service_type"] = orders["service_type"].astype("category")
    orders["_input_ordinal"] = np.arange(len(orders), dtype=np.int64)
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

    if "_input_ordinal" not in carpool:
        carpool["_input_ordinal"] = np.arange(len(carpool), dtype=np.int64)
    carpool = carpool.sort_values(
        ["driver_id", "departure_time", "finish_time", "order_id", "_input_ordinal"],
        kind="mergesort",
        na_position="last",
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

    if "_input_ordinal" not in exclusive:
        exclusive["_input_ordinal"] = np.arange(len(exclusive), dtype=np.int64)
    segment_ids = [
        f"order:null:{ordinal}" if pd.isna(order_id) else f"order:text:{len(str(order_id))}:{order_id}"
        for order_id, ordinal in zip(exclusive["order_id"], exclusive["_input_ordinal"])
    ]
    exclusive = exclusive.assign(
        segment_id=segment_ids,
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
    """Assign driver chain identifiers using vectorized shifts and cumulative sums.

    Chains split ONLY when the inter-trip gap exceeds ``max_gap_minutes``. The
    former midnight break (``date_ != prev_date``) is removed, so a short
    cross-midnight gap (<= max_gap) stays one continuous chain and a long one
    (> max_gap) still splits. ``chain_id`` no longer encodes a calendar date (a
    chain may span two days); it is ``driver_id`` + a per-driver chain sequence.
    """
    if trip_segments.empty:
        chains = pd.DataFrame(
            columns=[
                "chain_id",
                "driver_id",
                "chain_seq",
                "chain_start",
                "chain_end",
                "segment_count",
                "segment_ids",
            ]
        )
        return trip_segments.assign(chain_id=pd.Series(dtype="object")), chains

    # build_trip_segments already returns rows sorted by [driver_id, trip_start,
    # trip_end, segment_id] with a clean RangeIndex (proven by
    # test_build_trip_segments_sorted_precondition), so a re-sort here is
    # redundant. Sort only as a defensive fallback if a caller passed unsorted
    # input (detected via a cheap necessary-condition check on the leading key).
    sort_keys = ["driver_id", "trip_start", "trip_end", "segment_id"]
    if not trip_segments["driver_id"].is_monotonic_increasing:
        trip_segments = trip_segments.sort_values(sort_keys, kind="mergesort")
    segments = trip_segments.reset_index(drop=True)
    prev_end = segments.groupby("driver_id")["trip_end"].shift()
    gap_min = (segments["trip_start"] - prev_end).dt.total_seconds().div(60)
    new_chain = prev_end.isna() | (gap_min > max_gap_minutes)
    segments["chain_seq"] = new_chain.groupby(segments["driver_id"]).cumsum().astype("int64")
    segments["chain_id"] = (
        segments["driver_id"].astype(str) + "_" + segments["chain_seq"].astype(str)
    )

    grouped = segments.groupby(["chain_id", "driver_id", "chain_seq"], sort=False)
    chains = grouped.agg(
        chain_start=("trip_start", "first"),
        chain_end=("trip_end", "last"),
        segment_count=("segment_id", "size"),
        segment_ids=("segment_id", list),
    ).reset_index()

    chain_counts = chains.groupby("driver_id", as_index=False)["chain_id"].nunique()
    anomalies = chain_counts.loc[chain_counts["chain_id"] > 200]
    if not anomalies.empty:
        LOGGER.warning("Drivers with >200 chains over the window: %d drivers.", len(anomalies))
    return segments, chains


def extract_idle_windows(
    chain_segments: pd.DataFrame,
    tau_idle_minutes: int = TAU_IDLE_MINUTES,
) -> pd.DataFrame:
    """Extract positive inter-trip idle windows inside reconstructed chains.

    ``tau_idle_minutes`` is the idle-judgement cap (a gap shorter than it counts as
    online-idle -> ``available``); it is DISTINCT from the chain-formation gap
    ``max_gap_minutes`` used in ``reconstruct_driver_chains``. Decoupled so the
    idle cap can be tightened (60 -> 30) without changing chain structure (hence
    leaving trip_segments / in-service / fleet's in-service part untouched)."""
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
        & (idle["idle_duration_s"] <= tau_idle_minutes * 60)
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
    needed = [start_col, end_col, *columns]
    intervals = frame.loc[frame[end_col] > frame[start_col], needed].copy()
    if intervals.empty:
        return pd.DataFrame(columns=output_columns)

    start = intervals[start_col]
    end = intervals[end_col]
    intervals["_slot_start"] = start.dt.floor(frequency)
    intervals["_slot_end"] = (end - pd.Timedelta(nanoseconds=1)).dt.floor(frequency)
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

    # Fix-2: an in-service trip attributes its driver to the ORIGIN cluster only.
    # Counting the destination cluster too double-counts the driver across two
    # clusters in the same slot and inflates the per-cluster fleet lower bound.
    origin_activity = trip_driver_slots[["slot_start", "driver_id", "origin_cluster_id"]].rename(
        columns={"origin_cluster_id": "cluster_id"}
    )
    activity_frames = [frame for frame in [idle_activity, origin_activity] if not frame.empty]
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
    # Restrict the dense grid to slots that actually carry activity (drops all-zero
    # edge slots that generate_slots' ceil(max trip_end) can introduce).
    observed = pd.to_datetime(
        pd.concat(
            [available["slot_start"], in_service["slot_start"], fleet["slot_start"]],
            ignore_index=True,
        ),
        errors="coerce",
    ).dropna()
    if not slots.empty and not observed.empty:
        slots = slots[(slots["slot_start"] >= observed.min()) & (slots["slot_start"] <= observed.max())]
    clusters = build_cluster_universe(trip_segments, idle_windows)
    available = complete_slot_cluster_grid(available, slots, clusters, ["available_vehicles"])
    fleet = attach_global_fleet_to_all_clusters(fleet, slots, clusters)
    return available, in_service, fleet


def serialize_list_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Convert list-valued columns to JSON strings for CSV persistence.

    Scalar output tables (no ``order_ids``/``segment_ids`` columns) are returned
    unchanged without a full-frame ``.copy()``; only frames that actually carry a
    list column are copied and JSON-encoded. The persisted CSV is identical either
    way (the copy was a pure RSS overhead for the scalar tables)."""
    list_columns = [column for column in ("order_ids", "segment_ids") if column in frame.columns]
    if not list_columns:
        return frame
    output = frame.copy()
    for column in list_columns:
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
    tau_idle_minutes: int = TAU_IDLE_MINUTES,
) -> dict[str, pd.DataFrame]:
    """Process one in-memory order frame into all supply-side output tables.

    ``max_gap_minutes`` forms chains; ``tau_idle_minutes`` (distinct) caps idle
    windows -> available. In-service depends only on ``trip_segments`` and is
    independent of both gap thresholds."""
    trip_segments = build_trip_segments(orders, carpool_merge_gap_s)
    chain_segments, driver_chains = reconstruct_driver_chains(trip_segments, max_gap_minutes)
    idle_windows = extract_idle_windows(chain_segments, tau_idle_minutes)
    available, in_service, fleet = compute_supply_variables(trip_segments, idle_windows, slot_duration_min)
    return {
        "trip_segments": trip_segments,
        "driver_chains": driver_chains,
        "idle_windows": idle_windows,
        "supply_available_floor": available,
        "supply_inservice_od": in_service,
        "supply_fleet_lower_bound": fleet,
    }


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

    available = pd.read_csv(output_dir / "supply_available_floor.csv.gz")
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
    tau_idle_minutes: int = TAU_IDLE_MINUTES,
    n_blocks: int = DRIVER_BLOCKS,
) -> dict[str, object]:
    """Run the supply-side reconstruction (per-driver-chunked) and write all outputs."""
    output_dir = Path(output_dir)
    orders = load_orders(orders_path)
    configure_file_logging(output_dir)
    config_used = {
        "orders_path": str(orders_path),
        "output_dir": str(output_dir),
        "max_gap_minutes": max_gap_minutes,
        "tau_idle_minutes": tau_idle_minutes,
        "carpool_merge_gap_s": carpool_merge_gap_s,
        "slot_duration_min": slot_duration_min,
        "merge_demand": merge_demand,
        "demand_table": demand_table,
        "n_blocks": n_blocks,
    }
    write_json(config_used, output_dir / "config_used.json")
    return _run_chunked_pipeline(
        orders=orders,
        output_dir=output_dir,
        max_gap_minutes=max_gap_minutes,
        carpool_merge_gap_s=carpool_merge_gap_s,
        slot_duration_min=slot_duration_min,
        n_blocks=n_blocks,
        merge_demand=merge_demand,
        demand_table=demand_table,
        tau_idle_minutes=tau_idle_minutes,
    )


# ==========================================================================
# Driver-chunked execution skeleton (the (B) refactor).
#
# Splits drivers into DRIVER_BLOCKS uniform-hash buckets, runs the SAME per-frame
# compute (build_trip_segments -> reconstruct_driver_chains -> extract_idle_windows
# -> compute_* ) on each block, and accumulates the block-level results into dense
# global arrays. No computation/threshold/naming semantics change -- only the
# execution skeleton. Correctness rests on four invariants (asserted at runtime):
#   1. Block assignment is ONE uniform-hash function (driver_block_id).
#   2. Each driver is in exactly one block (disjoint + complete) -> per-block
#      nunique is additive across blocks.
#   3. A single global slot/cluster index built ONCE; blocks map their
#      (slot, cluster) to global positions before accumulating (no local indices).
#   4. fleet does the in-service-union-idle driver dedup INSIDE each block, then
#      blocks are summed (disjoint drivers) -- never global-tensor add afterwards.
# ==========================================================================
def driver_block_id(driver_ids: "np.ndarray | pd.Series", n_blocks: int) -> np.ndarray:
    """Sole block-assignment function (invariant 1). Uniform 64-bit hash of the
    driver id, modulo n_blocks -- never a bare ``driver_id % n`` (those ids cluster
    in low bits: an earlier probe showed ``%16`` captured 21.6% instead of 6.25%)."""
    if n_blocks <= 0:
        raise ValueError("n_blocks must be positive")
    values = np.asarray(driver_ids)
    unique = pd.unique(values)
    blocks = {
        value: int.from_bytes(hashlib.sha256(str(value).encode("utf-8")).digest()[:8], "big") % n_blocks
        for value in unique
    }
    return np.fromiter((blocks[value] for value in values), dtype=np.int64, count=len(values))


def build_global_slot_index(orders: pd.DataFrame, slot_duration_min: int) -> pd.DatetimeIndex:
    """Global continuous slot set over the whole study window, built ONCE (invariant 3).

    [floor(min departure), ceil(max finish)) stepped by the slot width -- the same
    first/last logic as ``generate_slots``, but global across all blocks. trip_start
    = min(departure) and trip_end = max(finish) within a carpool group, and idle
    windows live between trips, so every emitted slot is inside this range."""
    frequency = f"{slot_duration_min}min"
    first = orders["departure_time"].min().floor(frequency)
    last = orders["finish_time"].max().ceil(frequency)
    return pd.date_range(first, last, freq=frequency, inclusive="left")


def build_global_cluster_index(orders: pd.DataFrame) -> pd.Index:
    """Global cluster set (all clusters observed as an origin or destination), built
    ONCE (invariant 3). Sorted so the column/row order is stable across blocks."""
    values = pd.concat(
        [orders["origin_cluster_id"], orders["destination_cluster_id"]], ignore_index=True
    ).dropna().unique()
    return pd.Index(np.sort(values), name="cluster_id")


def _slot_positions(slot_series: pd.Series, slots: pd.DatetimeIndex) -> np.ndarray:
    pos = slots.get_indexer(pd.to_datetime(slot_series))
    if (pos < 0).any():
        raise AssertionError("invariant 3 violated: a block slot is outside the global slot index.")
    return pos


def _cluster_positions(cluster_series: pd.Series, clusters: pd.Index) -> np.ndarray:
    pos = clusters.get_indexer(cluster_series)
    if (pos < 0).any():
        raise AssertionError("invariant 3 violated: a block cluster is outside the global cluster index.")
    return pos


def run_chunked_pipeline(
    orders_path: str | Path = ORDERS_PATH,
    output_dir: str | Path = OUTPUT_DIR,
    max_gap_minutes: int = MAX_GAP_MINUTES,
    carpool_merge_gap_s: int = CARPOOL_MERGE_GAP_S,
    slot_duration_min: int = SLOT_DURATION_MIN,
    n_blocks: int = DRIVER_BLOCKS,
    merge_demand: bool = MERGE_WITH_DEMAND,
    demand_table: str = DEMAND_TABLE,
    tau_idle_minutes: int = TAU_IDLE_MINUTES,
) -> dict[str, object]:
    """Validate Demand's artifact, then run driver-chunked Supply."""
    return _run_chunked_pipeline(
        orders=load_orders(orders_path),
        output_dir=output_dir,
        max_gap_minutes=max_gap_minutes,
        carpool_merge_gap_s=carpool_merge_gap_s,
        slot_duration_min=slot_duration_min,
        n_blocks=n_blocks,
        merge_demand=merge_demand,
        demand_table=demand_table,
        tau_idle_minutes=tau_idle_minutes,
    )


def _run_chunked_pipeline(
    *,
    orders: pd.DataFrame,
    output_dir: str | Path,
    max_gap_minutes: int,
    carpool_merge_gap_s: int,
    slot_duration_min: int,
    n_blocks: int,
    merge_demand: bool,
    demand_table: str,
    tau_idle_minutes: int,
) -> dict[str, object]:
    """Compute Supply from an already validated frame without reloading it."""
    output_dir = Path(output_dir)
    LOGGER.info("Loaded %d orders for driver-chunked run (%d blocks).", len(orders), n_blocks)

    orders["_block"] = driver_block_id(orders["driver_id"], n_blocks)

    # Invariant 3: global indices, built ONCE, shared by every block.
    slots = build_global_slot_index(orders, slot_duration_min)
    clusters = build_global_cluster_index(orders)
    T, N = len(slots), len(clusters)
    LOGGER.info("Global grid: T=%d slots x N=%d clusters.", T, N)

    # Dense accumulators in int32. Counts are non-negative and << 2^31 (the
    # per-block negative-wrap check below catches any int32 overflow the moment
    # it happens), so int32 suffices and halves resident RSS vs int64. Output
    # frames still cast to int64, so persisted CSV values are unchanged.
    A = np.zeros((T, N, N), dtype=np.int32)       # in-service: slot x origin x dest
    B = np.zeros((T, N), dtype=np.int32)          # available: slot x cluster
    Fc = np.zeros((T, N), dtype=np.int32)          # fleet lower bound per cluster
    Fg = np.zeros(T, dtype=np.int32)               # global fleet lower bound per slot

    all_drivers = set(orders["driver_id"].unique())
    seen_drivers: set = set()
    block_summaries = []

    for b in range(n_blocks):
        block = orders.loc[orders["_block"] == b].drop(columns="_block")
        block_drivers = set(block["driver_id"].unique())
        # Invariant 2: disjoint blocks (assert before processing).
        if not seen_drivers.isdisjoint(block_drivers):
            raise AssertionError(f"invariant 2 violated: block {b} shares drivers with an earlier block.")
        seen_drivers |= block_drivers

        # Same per-frame compute path as process_orders_frame (no semantic change).
        trip_segments = build_trip_segments(block, carpool_merge_gap_s)
        chain_segments, _ = reconstruct_driver_chains(trip_segments, max_gap_minutes)
        idle_windows = extract_idle_windows(chain_segments, tau_idle_minutes)
        available, idle_driver_slots = compute_available_by_cluster(idle_windows, slot_duration_min, True)
        in_service, trip_driver_slots = compute_in_service_od(trip_segments, slot_duration_min, True)
        # Invariant 4: dedup in-service-union-idle WITHIN the block, then block-sum.
        fleet = compute_fleet_lower_bound(idle_driver_slots, trip_driver_slots)

        # Accumulate via global index maps (invariant 3). Indices are duplicate-
        # free within each call (groupby().nunique() / drop_duplicates upstream),
        # so the buffered += is equivalent to np.add.at and faster (no unbuffered
        # scatter); cross-block accumulation is unchanged (each call is a correct
        # increment onto the shared dense accumulator).
        if not in_service.empty:
            t = _slot_positions(in_service["slot_start"], slots)
            i = _cluster_positions(in_service["origin_cluster_id"], clusters)
            j = _cluster_positions(in_service["destination_cluster_id"], clusters)
            A[t, i, j] += in_service["vehicles_in_service"].to_numpy()
            if (A[t, i, j] < 0).any():
                raise AssertionError("accumulator A overflowed int32 (in-service counts).")
        if not available.empty:
            t = _slot_positions(available["slot_start"], slots)
            c = _cluster_positions(available["cluster_id"], clusters)
            B[t, c] += available["available_vehicles"].to_numpy()
            if (B[t, c] < 0).any():
                raise AssertionError("accumulator B overflowed int32 (available counts).")
        if not fleet.empty:
            t = _slot_positions(fleet["slot_start"], slots)
            c = _cluster_positions(fleet["cluster_id"], clusters)
            Fc[t, c] += fleet["fleet_lower_bound_cluster"].to_numpy()
            if (Fc[t, c] < 0).any():
                raise AssertionError("accumulator Fc overflowed int32 (fleet per-cluster counts).")
            g = fleet[["slot_start", "global_fleet_lower_bound"]].drop_duplicates("slot_start")
            tg = _slot_positions(g["slot_start"], slots)
            Fg[tg] += g["global_fleet_lower_bound"].to_numpy()
            if (Fg[tg] < 0).any():
                raise AssertionError("accumulator Fg overflowed int32 (global fleet counts).")

        block_summaries.append({"block": b, "orders": int(len(block)), "drivers": int(len(block_drivers)),
                                "in_service_rows": int(len(in_service))})
        LOGGER.info("Block %d/%d done: orders=%d drivers=%d.", b + 1, n_blocks, len(block), len(block_drivers))
        del block, trip_segments, chain_segments, idle_windows
        del available, in_service, fleet, idle_driver_slots, trip_driver_slots

    # Invariant 2: completeness (union of blocks == all drivers).
    if seen_drivers != all_drivers:
        raise AssertionError("invariant 2 violated: union of block drivers != all drivers.")
    # int32 overflow is detected per-block above (a negative value after a += is
    # a wrap); there is no post-loop range check because int32 storage cannot
    # exceed int32.max by construction (a wrapped value is still <= int32.max).

    # Dense arrays -> output frames (same schema/keys as the other modes).
    in_service_df = _inservice_array_to_frame(A, slots, clusters)
    available_df = _dense_cluster_array_to_frame(B, slots, clusters, "available_vehicles")
    fleet_df = _fleet_arrays_to_frame(Fc, Fg, slots, clusters)

    save_csv_gz(in_service_df, output_dir / "supply_inservice_od.csv.gz")
    save_csv_gz(available_df, output_dir / "supply_available_floor.csv.gz")
    save_csv_gz(fleet_df, output_dir / "supply_fleet_lower_bound.csv.gz")
    if merge_demand:
        merge_supply_with_demand(output_dir, demand_table)

    summary = {
        "execution_mode": "driver-chunked",
        "max_gap_minutes": max_gap_minutes,
        "tau_idle_minutes": tau_idle_minutes,
        "n_blocks": n_blocks,
        "orders_loaded": int(len(orders)),
        "n_drivers": int(len(all_drivers)),
        "global_slots": int(T),
        "global_clusters": int(N),
        "available_rows": int(len(available_df)),
        "in_service_rows": int(len(in_service_df)),
        "fleet_rows": int(len(fleet_df)),
        "note": "intermediate trip_segments/driver_chains/idle_windows are not dumped in "
                "chunked mode (object-column residency is the memory hog); supply products only.",
        "block_summaries": block_summaries,
    }
    write_json(summary, output_dir / "run_summary.json")
    LOGGER.info("Driver-chunked summary: %s", {k: v for k, v in summary.items() if k != "block_summaries"})
    return summary


def _inservice_array_to_frame(A: np.ndarray, slots: pd.DatetimeIndex, clusters: pd.Index) -> pd.DataFrame:
    """Dense [T,N,N] -> sparse (slot, origin, dest, vehicles_in_service) rows (nonzero only)."""
    tt, ii, jj = np.nonzero(A)
    if tt.size == 0:
        return pd.DataFrame(columns=IN_SERVICE_COLUMNS)
    frame = pd.DataFrame({
        "slot_start": slots.to_numpy()[tt],
        "origin_cluster_id": clusters.to_numpy()[ii],
        "destination_cluster_id": clusters.to_numpy()[jj],
        "vehicles_in_service": A[tt, ii, jj].astype("int64"),
    })
    return frame.sort_values(["slot_start", "origin_cluster_id", "destination_cluster_id"]).reset_index(drop=True)


def _dense_cluster_array_to_frame(M: np.ndarray, slots: pd.DatetimeIndex, clusters: pd.Index, value_col: str) -> pd.DataFrame:
    """Dense [T,N] -> (slot, cluster, value) rows over the full global grid."""
    T, N = M.shape
    return pd.DataFrame({
        "slot_start": np.repeat(slots.to_numpy(), N),
        "cluster_id": np.tile(clusters.to_numpy(), T),
        value_col: M.reshape(-1).astype("int64"),
    })


def _fleet_arrays_to_frame(Fc: np.ndarray, Fg: np.ndarray, slots: pd.DatetimeIndex, clusters: pd.Index) -> pd.DataFrame:
    """Dense [T,N] cluster bound + per-slot global bound -> fleet rows over the full grid."""
    frame = _dense_cluster_array_to_frame(Fc, slots, clusters, "fleet_lower_bound_cluster")
    T, N = Fc.shape
    frame["global_fleet_lower_bound"] = np.repeat(Fg, N).astype("int64")
    return frame[FLEET_COLUMNS]


def run_supply(config: ResolvedStageConfig, context: RunContext) -> StageResult:
    """Run Supply inside its owned run directory without updating a manifest."""
    if context.stage_dir is None or context.stage_name != "supply":
        raise ValueError("run_supply requires context.for_stage('supply')")
    values = deepcopy(dict(config.values))
    if "stage3_supply" not in values:
        raise ValueError("configuration must contain a stage3_supply section")
    stage = deepcopy(dict(values["stage3_supply"]))
    output_dir = context.stage_dir
    orders_path = resolve_path(stage["orders_path"], base_dir=config.source_path.parent)
    summary = run_pipeline(
        orders_path=orders_path,
        output_dir=output_dir,
        max_gap_minutes=int(stage["max_gap_minutes"]),
        tau_idle_minutes=int(stage.get("tau_idle_minutes", TAU_IDLE_MINUTES)),
        carpool_merge_gap_s=int(stage["carpool_merge_gap_s"]),
        slot_duration_min=int(stage["slot_duration_min"]),
        n_blocks=int(stage.get("n_blocks", DRIVER_BLOCKS)),
    )
    names = {
        "inservice_od": "supply_inservice_od.csv.gz",
        "available_floor": "supply_available_floor.csv.gz",
        "fleet_lower_bound": "supply_fleet_lower_bound.csv.gz",
        "run_summary": "run_summary.json",
        "config_used": "config_used.json",
    }
    outputs = {name: output_dir / filename for name, filename in names.items()}
    missing = [path.name for path in outputs.values() if not path.is_file()]
    if missing:
        raise RuntimeError(f"Supply completed without required outputs: {missing}")
    return StageResult(
        stage="supply",
        status=StageStatus.COMPLETE,
        outputs=outputs,
        metrics={
            "orders": int(summary["orders_loaded"]),
            "drivers": int(summary["n_drivers"]),
            "slots": int(summary["global_slots"]),
            "clusters": int(summary["global_clusters"]),
            "inservice_rows": int(summary["in_service_rows"]),
            "available_rows": int(summary["available_rows"]),
            "fleet_rows": int(summary["fleet_rows"]),
        },
    )
