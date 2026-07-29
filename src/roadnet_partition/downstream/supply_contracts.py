from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


TABLES = {
    "inservice_od": {
        "filename": "supply_inservice_od.csv.gz",
        "columns": ["slot_start", "origin_cluster_id", "destination_cluster_id", "vehicles_in_service"],
        "keys": ["slot_start", "origin_cluster_id", "destination_cluster_id"],
        "values": ["vehicles_in_service"],
    },
    "available_floor": {
        "filename": "supply_available_floor.csv.gz",
        "columns": ["slot_start", "cluster_id", "available_vehicles"],
        "keys": ["slot_start", "cluster_id"],
        "values": ["available_vehicles"],
    },
    "fleet_lower_bound": {
        "filename": "supply_fleet_lower_bound.csv.gz",
        "columns": ["slot_start", "cluster_id", "fleet_lower_bound_cluster", "global_fleet_lower_bound"],
        "keys": ["slot_start", "cluster_id"],
        "values": ["fleet_lower_bound_cluster", "global_fleet_lower_bound"],
    },
}


def _validate_table(
    path: Path,
    columns: list[str],
    keys: list[str],
    values: list[str],
    cluster_columns: Iterable[str],
    chunksize: int,
) -> dict[str, Any]:
    rows = 0
    previous_key: tuple[Any, ...] | None = None
    time_min = None
    time_max = None
    clusters: set[str] = set()
    totals = {column: 0 for column in values}
    key_hash = hashlib.sha256()
    for chunk in pd.read_csv(path, chunksize=chunksize, dtype={column: str for column in cluster_columns}):
        if list(chunk.columns) != columns:
            raise ValueError(f"{path.name} schema differs: {list(chunk.columns)}")
        if chunk[keys].isna().any().any() or chunk[values].isna().any().any():
            raise ValueError(f"{path.name} contains null key or value fields")
        for column in values:
            if not pd.api.types.is_integer_dtype(chunk[column].dtype):
                raise ValueError(f"{path.name} {column} must be integer")
            if (chunk[column] < 0).any():
                raise ValueError(f"{path.name} {column} contains negative values")
            totals[column] += int(chunk[column].sum())
        index = pd.MultiIndex.from_frame(chunk[keys])
        if not index.is_unique or not index.is_monotonic_increasing:
            raise ValueError(f"{path.name} primary key is duplicate or unsorted")
        if len(index):
            first_key = tuple(index[0])
            last_key = tuple(index[-1])
            if previous_key is not None and first_key <= previous_key:
                raise ValueError(f"{path.name} primary key is not increasing across chunks")
            previous_key = last_key
        slots = chunk["slot_start"].astype(str)
        if len(slots):
            time_min = slots.min() if time_min is None else min(time_min, slots.min())
            time_max = slots.max() if time_max is None else max(time_max, slots.max())
        for column in cluster_columns:
            clusters.update(chunk[column].astype(str))
        key_hash.update(pd.util.hash_pandas_object(chunk[keys], index=False).to_numpy().tobytes())
        rows += len(chunk)
    return {
        "rows": rows,
        "time_min": time_min,
        "time_max": time_max,
        "clusters": sorted(
            clusters,
            key=lambda value: (0, int(value)) if value.lstrip("-").isdigit() else (1, value),
        ),
        "totals": totals,
        "key_hash": key_hash.hexdigest(),
    }


def validate_supply_outputs(
    output_dir: Path,
    *,
    expected_cluster_ids: Iterable[Any] | None = None,
    expected_config: dict[str, Any] | None = None,
    chunksize: int = 500_000,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    required_files = [spec["filename"] for spec in TABLES.values()] + ["run_summary.json", "config_used.json"]
    missing = [filename for filename in required_files if not (output_dir / filename).is_file()]
    if missing:
        if (output_dir / "run_summary.partial.json").exists():
            raise ValueError(f"partial summary is not a completed Supply contract; missing={missing}")
        raise FileNotFoundError(f"Supply contract files are missing: {missing}")
    results = {
        "inservice_od": _validate_table(
            output_dir / TABLES["inservice_od"]["filename"],
            TABLES["inservice_od"]["columns"],
            TABLES["inservice_od"]["keys"],
            TABLES["inservice_od"]["values"],
            ["origin_cluster_id", "destination_cluster_id"],
            chunksize,
        ),
        "available_floor": _validate_table(
            output_dir / TABLES["available_floor"]["filename"],
            TABLES["available_floor"]["columns"],
            TABLES["available_floor"]["keys"],
            TABLES["available_floor"]["values"],
            ["cluster_id"],
            chunksize,
        ),
        "fleet_lower_bound": _validate_table(
            output_dir / TABLES["fleet_lower_bound"]["filename"],
            TABLES["fleet_lower_bound"]["columns"],
            TABLES["fleet_lower_bound"]["keys"],
            TABLES["fleet_lower_bound"]["values"],
            ["cluster_id"],
            chunksize,
        ),
    }
    available = results["available_floor"]
    fleet = results["fleet_lower_bound"]
    inservice = results["inservice_od"]
    if available["key_hash"] != fleet["key_hash"] or available["rows"] != fleet["rows"]:
        raise ValueError("available and fleet slot/cluster axes differ")
    if (inservice["time_min"], inservice["time_max"]) != (available["time_min"], available["time_max"]):
        raise ValueError("Supply table time ranges differ")
    dense_clusters = set(available["clusters"])
    if set(fleet["clusters"]) != dense_clusters or not set(inservice["clusters"]).issubset(dense_clusters):
        raise ValueError("Supply table cluster sets differ")
    if expected_cluster_ids is not None and dense_clusters != {str(value) for value in expected_cluster_ids}:
        raise ValueError("Supply clusters differ from the expected Demand cluster set")

    summary = json.loads((output_dir / "run_summary.json").read_text(encoding="utf-8"))
    required_summary = {
        "execution_mode", "max_gap_minutes", "tau_idle_minutes", "n_blocks",
        "orders_loaded", "n_drivers", "global_slots", "global_clusters",
        "available_rows", "in_service_rows", "fleet_rows", "note", "block_summaries",
    }
    if set(summary) != required_summary or summary["execution_mode"] != "driver-chunked":
        raise ValueError("run_summary schema or execution mode differs")
    row_fields = {
        "available_rows": available["rows"],
        "in_service_rows": inservice["rows"],
        "fleet_rows": fleet["rows"],
    }
    for field, rows in row_fields.items():
        if int(summary[field]) != rows:
            raise ValueError(f"run_summary {field} differs from its CSV")
    if int(summary["global_clusters"]) != len(dense_clusters):
        raise ValueError("run_summary global_clusters differs from CSV clusters")
    if available["rows"] != int(summary["global_slots"]) * int(summary["global_clusters"]):
        raise ValueError("available output is not the complete formal slot/cluster grid")

    config_used = json.loads((output_dir / "config_used.json").read_text(encoding="utf-8"))
    required_config = {
        "orders_path", "output_dir", "max_gap_minutes", "tau_idle_minutes",
        "carpool_merge_gap_s", "slot_duration_min", "merge_demand", "demand_table", "n_blocks",
    }
    if set(config_used) != required_config:
        raise ValueError("config_used schema differs")
    if expected_config is not None:
        for key, value in expected_config.items():
            if config_used.get(key) != value:
                raise ValueError(f"config_used {key} differs from the resolved run config")
    return {**results, "run_summary": summary, "config_used": config_used}
