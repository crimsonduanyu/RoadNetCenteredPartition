"""Complete-path per-driver-block memory probe for the (B) chunking refactor.

Answers ONE parameter question: how many drivers per block keeps the whole
process peak inside a 32 GB budget (60% safety waterline = ~19 GB)?

This walks the REAL pipeline path (the same functions process_orders_frame calls),
so it actually exercises the three blind spots the earlier light probes missed:
  (1) slot expansion blow-up (_expand_interval_slots: trip segments -> N*rows),
  (2) object-column residency (trip_segments.order_ids list-col, segment_id str),
  (3) the cross-block aggregator that lives for the whole batch.

Design: each block is run in its OWN subprocess so PeakWorkingSet reflects only
that block's load+processing (a real chunk worker), uncontaminated by a full-file
load. A block = drivers with (driver_id % k == 0); ALL of a driver's orders land
in one block (drivers never cross blocks), matching the chunking invariant.

Read-only: loads order data and calls existing supply functions; modifies nothing.

Usage:
  python scripts/analysis/supply_block_mem_probe.py            # orchestrator
  python scripts/analysis/supply_block_mem_probe.py --block K  # one block (subprocess)
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import gc
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SUPPLY_PY = ROOT / "src" / "lib" / "supply.py"
ORDERS = ROOT / "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
DAILY_SUMMARY = ROOT / "data/processed/fifth_ring/supply/run_summary.json"
OUT_DIR = ROOT / "outputs/analysis"
TIME_FMT = "%Y-%m-%d %H:%M:%S"
CHUNK = 4_000_000
GB = 1024 ** 3
BUDGET_GB = 32.0
WATERLINE_GB = BUDGET_GB * 0.60  # ~19.2 GB
RATIOS = [4, 8, 16]  # blocks counts N to probe (1/4, 1/8, 1/16 of drivers)

USECOLS = ["order_id", "driver_id", "departure_time", "finish_time",
           "origin_cluster_id", "destination_cluster_id", "service_type"]
DTYPES = {"order_id": "int64", "driver_id": "int64", "origin_cluster_id": "int32",
          "destination_cluster_id": "int32", "service_type": "category",
          "departure_time": "string", "finish_time": "string"}


# ---------------- Windows peak working-set API ----------------
class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t),
                ("d", ctypes.c_size_t), ("e", ctypes.c_size_t), ("f", ctypes.c_size_t)]


_K32 = ctypes.windll.kernel32
_K32.GetCurrentProcess.restype = ctypes.c_void_p
_PMI = _K32.K32GetProcessMemoryInfo
_PMI.restype = wintypes.BOOL
_PMI.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]


def mem() -> tuple[float, float]:
    c = PMC(); c.cb = ctypes.sizeof(c)
    _PMI(_K32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.WorkingSetSize / GB, c.PeakWorkingSetSize / GB


def load_supply():
    spec = importlib.util.spec_from_file_location("supply_live", SUPPLY_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def deep_gb(*frames) -> float:
    return float(sum(f.memory_usage(deep=True).sum() for f in frames)) / GB


def load_block(k: int) -> pd.DataFrame:
    """Stream-read only this block's drivers (driver_id % k == 0), real dtypes."""
    parts = []
    for ch in pd.read_csv(ORDERS, usecols=USECOLS, dtype=DTYPES, chunksize=CHUNK):
        # Uniform value-based bucketing: driver_id % k is badly skewed (these IDs
        # cluster in low bits), so use a well-mixed 64-bit hash of driver_id. The
        # hash is deterministic per value, so a driver always lands in one block.
        h = pd.util.hash_array(ch["driver_id"].to_numpy())
        keep = (h % k) == 0
        if keep.any():
            parts.append(ch.loc[keep])
    df = pd.concat(parts, ignore_index=True)
    del parts
    for col in ("departure_time", "finish_time"):
        df[col] = pd.to_datetime(df[col], format=TIME_FMT, errors="coerce")
    gc.collect()
    return df


def probe_block(k: int) -> dict:
    """Run the real pipeline path on one block, instrumenting each heavy stage."""
    supply = load_supply()
    SLOT = supply.SLOT_DURATION_MIN
    MAXGAP = supply.MAX_GAP_MINUTES
    CARP = supply.CARPOOL_MERGE_GAP_S

    orders = load_block(k)
    n_orders = len(orders)
    n_drivers = int(orders["driver_id"].nunique())
    a_orders_gb = deep_gb(orders)
    _, peak_after_load = mem()

    # --- mirror process_orders_frame, stepwise, to attribute peaks ---
    trip_segments = supply.build_trip_segments(orders, CARP)
    trip_gb = deep_gb(trip_segments)
    chain_segments, driver_chains = supply.reconstruct_driver_chains(trip_segments, MAXGAP)
    idle_windows = supply.extract_idle_windows(chain_segments, MAXGAP)
    b_obj_resident_gb = deep_gb(trip_segments, idle_windows, chain_segments)

    # (c) slot expansion: the two expanded driver-slot frames (the 113M/N step)
    available, idle_driver_slots = supply.compute_available_by_cluster(idle_windows, SLOT, True)
    _, peak_after_idle_expand = mem()
    idle_slots_gb = deep_gb(idle_driver_slots)
    in_service, trip_driver_slots = supply.compute_in_service_od(trip_segments, SLOT, True)
    _, peak_after_trip_expand = mem()
    trip_slots_gb = deep_gb(trip_driver_slots)
    c_expand_peak_gb = max(idle_slots_gb, trip_slots_gb)
    n_trip_slot_rows = int(len(trip_driver_slots))
    n_idle_slot_rows = int(len(idle_driver_slots))

    # finish fleet + grids (completes the real path)
    fleet = supply.compute_fleet_lower_bound(idle_driver_slots, trip_driver_slots)

    # block-level aggregator outputs (these are what cross-block agg accumulates)
    n_in_service_rows = int(len(in_service))      # distinct (slot, origin, dest)
    n_available_rows = int(len(available))        # distinct (slot, cluster)
    n_fleet_rows = int(len(fleet))                # distinct (slot, cluster)

    _, d_process_peak_gb = mem()
    return {
        "k_blocks": k,
        "block_orders": n_orders,
        "block_drivers": n_drivers,
        "a_orders_steady_gb": round(a_orders_gb, 3),
        "b_object_resident_gb": round(b_obj_resident_gb, 3),
        "b_trip_segments_only_gb": round(trip_gb, 3),
        "c_slot_expand_frame_gb": round(c_expand_peak_gb, 3),
        "c_trip_driver_slots_gb": round(trip_slots_gb, 3),
        "c_idle_driver_slots_gb": round(idle_slots_gb, 3),
        "d_process_peak_gb": round(d_process_peak_gb, 3),
        "peak_after_load_gb": round(peak_after_load, 3),
        "n_trip_slot_rows": n_trip_slot_rows,
        "n_idle_slot_rows": n_idle_slot_rows,
        "n_in_service_rows": n_in_service_rows,
        "n_available_rows": n_available_rows,
        "n_fleet_rows": n_fleet_rows,
    }


def run_block_subprocess(k: int) -> dict:
    out_path = OUT_DIR / f"block_probe_k{k}.json"
    cmd = [sys.executable, str(Path(__file__)), "--block", str(k)]
    print(f"[orchestrator] spawning block probe k={k} ...", flush=True)
    subprocess.run(cmd, check=True)
    return json.loads(out_path.read_text())


def orchestrate() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = [run_block_subprocess(k) for k in RATIOS]

    # Full-data scale references (for aggregator estimate, N-independent union size).
    daily = json.loads(DAILY_SUMMARY.read_text())
    full_in_service_rows = int(daily["in_service_rows"])   # ~ distinct (slot,OD) full
    full_available_rows = int(daily["available_rows"])

    # Aggregator = union of block outputs across all blocks == full distinct (slot,key).
    # Compact in-service agg row = datetime(8)+int16(2)+int16(2)+int64(8) = 20 bytes.
    inservice_agg_compact_gb = full_in_service_rows * 20 / GB
    inservice_agg_pandas_gb = full_in_service_rows * 40 / GB  # pandas overhead estimate
    # Dense [T,N,N] alternative: T slots * 100 * 100 cells.
    T_slots = 92 * 96
    dense_tnn_int32_gb = T_slots * 100 * 100 * 4 / GB
    available_agg_compact_gb = full_available_rows * 16 / GB

    aggregator_gb = inservice_agg_pandas_gb + available_agg_compact_gb * 2  # in_service + avail + fleet

    # Pick N: smallest k whose (block peak + aggregator) < waterline.
    recommendation = None
    for r in results:
        total = r["d_process_peak_gb"] + aggregator_gb
        r["block_plus_aggregator_gb"] = round(total, 3)
        r["fits_waterline"] = total < WATERLINE_GB
    fitting = [r for r in results if r["fits_waterline"]]
    if fitting:
        recommendation = min(fitting, key=lambda r: r["k_blocks"])  # smallest N that fits

    summary = {
        "budget_gb": BUDGET_GB,
        "waterline_gb": round(WATERLINE_GB, 2),
        "blocks_probed": results,
        "full_scale_refs": {
            "full_in_service_rows": full_in_service_rows,
            "full_available_rows": full_available_rows,
        },
        "aggregator_estimate": {
            "inservice_union_rows": full_in_service_rows,
            "inservice_agg_compact_gb": round(inservice_agg_compact_gb, 3),
            "inservice_agg_pandas_gb": round(inservice_agg_pandas_gb, 3),
            "dense_TxNxN_int32_gb": round(dense_tnn_int32_gb, 3),
            "available_fleet_agg_gb": round(available_agg_compact_gb * 2, 3),
            "aggregator_total_gb": round(aggregator_gb, 3),
            "needs_sparse": bool(dense_tnn_int32_gb > 1.0),
        },
        "recommended_N": recommendation["k_blocks"] if recommendation else None,
        "recommended_peak_plus_aggregator_gb": recommendation["block_plus_aggregator_gb"] if recommendation else None,
        "recommended_margin_gb": round(WATERLINE_GB - recommendation["block_plus_aggregator_gb"], 3) if recommendation else None,
    }
    (OUT_DIR / "supply_block_mem_probe_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False))
    print("\n================ BLOCK MEM PROBE SUMMARY ================")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", type=int, default=None)
    args = ap.parse_args()
    if args.block is not None:
        t0 = time.time()
        res = probe_block(args.block)
        res["elapsed_sec"] = round(time.time() - t0, 1)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / f"block_probe_k{args.block}.json").write_text(json.dumps(res, indent=2))
        print(json.dumps(res, indent=2))
    else:
        orchestrate()


if __name__ == "__main__":
    main()
