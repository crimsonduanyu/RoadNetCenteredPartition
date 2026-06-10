"""Verify tau_idle 60->30: A unchanged, available/fleet tighten (down only), B keeps info.

SLICE (fast, decisive): run whole-frame compute (== chunked, already proven) at
tau_idle=60 and =30, compare cell-for-cell:
  - in-service (A): MUST be identical (independent of tau_idle).
  - available (B): every cell B30 <= B60 (never up); report total drop + (30,60] sample.
  - fleet: F30 <= F60; report drop.
  - B-vs-A magnitude at tau=30 (does available still carry information?).
FULL: one chunked full run at tau_idle=30 -> process peak (< 19.2 GB?) + totals.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import importlib.util
import json
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SUPPLY_PY = ROOT / "src" / "lib" / "supply.py"
ORDERS = ROOT / "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
DAILY = ROOT / "data/processed/fifth_ring/supply"
OUT = ROOT / "outputs/analysis/step_tau_idle_verify_report.json"
USECOLS = ["order_id", "driver_id", "departure_time", "finish_time",
           "origin_cluster_id", "destination_cluster_id", "service_type"]
FMT = "%Y-%m-%d %H:%M:%S"
GB = 1024 ** 3


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("pf", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t)] + [(c, ctypes.c_size_t) for c in "abcdef"]


_K = ctypes.windll.kernel32
_K.GetCurrentProcess.restype = ctypes.c_void_p
_P = _K.K32GetProcessMemoryInfo
_P.restype = wintypes.BOOL
_P.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]


def peak_gb():
    c = PMC(); c.cb = ctypes.sizeof(c)
    _P(_K.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.PeakWorkingSetSize / GB


def load_supply():
    spec = importlib.util.spec_from_file_location("supply_live", SUPPLY_PY)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def read_slice(days):
    parts = []
    for ch in pd.read_csv(ORDERS, usecols=USECOLS, chunksize=2_000_000):
        dep = pd.to_datetime(ch["departure_time"], format=FMT, errors="coerce")
        keep = dep.dt.strftime("%Y-%m-%d").isin(days)
        if keep.any():
            sub = ch.loc[keep].copy()
            sub["departure_time"] = dep.loc[keep]
            sub["finish_time"] = pd.to_datetime(sub["finish_time"], format=FMT, errors="coerce")
            parts.append(sub)
    return pd.concat(parts, ignore_index=True)


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    supply = load_supply()
    rep = {"default_tau_idle": supply.TAU_IDLE_MINUTES, "default_max_gap": supply.MAX_GAP_MINUTES,
           "default_execution_mode": supply.EXECUTION_MODE}

    days = sorted(d["date"] for d in json.load(open(DAILY / "run_summary.json"))["daily_summaries"])[:4]
    orders = read_slice(set(days))
    rep["slice_days"], rep["slice_orders"] = days, int(len(orders))

    # whole-frame at tau=60 and tau=30 (max_gap fixed at 60 both times).
    o60 = supply.process_orders_frame(orders, 60, 0, 15, tau_idle_minutes=60)
    o30 = supply.process_orders_frame(orders, 60, 0, 15, tau_idle_minutes=30)

    def keyed(df, keys):
        d = df.copy(); d["slot_start"] = pd.to_datetime(d["slot_start"]); return d.set_index(keys)

    # (1) A identical (decisive).
    a60 = keyed(o60["supply_in_service_od"], ["slot_start", "origin_cluster_id", "destination_cluster_id"])["vehicles_in_service"]
    a30 = keyed(o30["supply_in_service_od"], ["slot_start", "origin_cluster_id", "destination_cluster_id"])["vehicles_in_service"]
    a_join = pd.concat([a60.rename("v60"), a30.rename("v30")], axis=1).fillna(0)
    rep["(1)_in_service_A"] = {
        "rows_60": int(len(a60)), "rows_30": int(len(a30)),
        "identical": bool((a_join["v60"] == a_join["v30"]).all()),
        "cells_differing": int((a_join["v60"] != a_join["v30"]).sum()),
    }

    # (2) available down only.
    b60 = keyed(o60["supply_available_by_cluster"], ["slot_start", "cluster_id"])["available_vehicles"]
    b30 = keyed(o30["supply_available_by_cluster"], ["slot_start", "cluster_id"])["available_vehicles"]
    bj = pd.concat([b60.rename("v60"), b30.rename("v30")], axis=1).fillna(0)
    increased = int((bj["v30"] > bj["v60"]).sum())
    rep["(2)_available_B"] = {
        "total_60": int(bj["v60"].sum()), "total_30": int(bj["v30"].sum()),
        "total_drop": int(bj["v60"].sum() - bj["v30"].sum()),
        "drop_pct": round(100.0 * (bj["v60"].sum() - bj["v30"].sum()) / bj["v60"].sum(), 2) if bj["v60"].sum() else None,
        "cells_increased_30_gt_60": increased,  # MUST be 0
        "cells_decreased": int((bj["v30"] < bj["v60"]).sum()),
    }

    # (2b) (30,60] idle-window sample: kept at 60, excluded at 30.
    i60 = o60["idle_windows"]; i30 = o30["idle_windows"]
    dur60 = i60["idle_duration_s"] / 60.0
    in_band = i60[(dur60 > 30) & (dur60 <= 60)]
    rep["(2b)_idle_band_30_60"] = {
        "idle_windows_60": int(len(i60)), "idle_windows_30": int(len(i30)),
        "idle_windows_dropped": int(len(i60) - len(i30)),
        "idle_windows_in_(30,60]_band": int(len(in_band)),
        "dropped_equals_band": bool((len(i60) - len(i30)) == len(in_band)),
        "sample_durations_min": [round(float(x), 1) for x in in_band["idle_duration_s"].head(5) / 60.0],
    }

    # (3) B vs A magnitude at tau=30 (does available still carry info?).
    rep["(3)_B_vs_A_magnitude_tau30"] = {
        "available_total_30": int(b30.sum()),
        "in_service_total_A": int(a30.sum()),
        "available_over_inservice_ratio": round(float(b30.sum()) / float(a30.sum()), 4) if a30.sum() else None,
    }

    # (4) fleet down.
    f60 = keyed(o60["supply_fleet_lower_bound"], ["slot_start", "cluster_id"])["fleet_lower_bound_cluster"]
    f30 = keyed(o30["supply_fleet_lower_bound"], ["slot_start", "cluster_id"])["fleet_lower_bound_cluster"]
    fj = pd.concat([f60.rename("v60"), f30.rename("v30")], axis=1).fillna(0)
    g60 = o60["supply_fleet_lower_bound"][["slot_start", "global_fleet_lower_bound"]].drop_duplicates()
    g30 = o30["supply_fleet_lower_bound"][["slot_start", "global_fleet_lower_bound"]].drop_duplicates()
    rep["(4)_fleet"] = {
        "cluster_total_60": int(fj["v60"].sum()), "cluster_total_30": int(fj["v30"].sum()),
        "cluster_cells_increased": int((fj["v30"] > fj["v60"]).sum()),  # MUST be 0
        "global_total_60": int(g60["global_fleet_lower_bound"].sum()),
        "global_total_30": int(g30["global_fleet_lower_bound"].sum()),
    }

    # (5) FULL run at default tau_idle=30 -> memory peak + totals.
    tmp = Path(tempfile.mkdtemp(prefix="tau30_full_"))
    t0 = time.time()
    full = supply.run_chunked_pipeline(orders_path=str(ORDERS), output_dir=str(tmp),
                                       max_gap_minutes=60, carpool_merge_gap_s=0, slot_duration_min=15,
                                       n_blocks=supply.DRIVER_BLOCKS, tau_idle_minutes=30)
    rep["(5)_full_run_tau30"] = {
        "process_peak_gb": round(peak_gb(), 2), "under_waterline_19_2": bool(peak_gb() < 19.2),
        "elapsed_sec": round(time.time() - t0, 1),
        "available_rows": full["available_rows"], "in_service_rows": full["in_service_rows"],
    }
    av = pd.read_csv(tmp / "supply_available_by_cluster.csv.gz", usecols=["available_vehicles"])
    insv = pd.read_csv(tmp / "supply_in_service_od.csv.gz", usecols=["vehicles_in_service"])
    rep["(5)_full_run_tau30"]["available_total"] = int(av["available_vehicles"].sum())
    rep["(5)_full_run_tau30"]["in_service_total"] = int(insv["vehicles_in_service"].sum())
    rep["(5)_full_run_tau30"]["available_over_inservice_ratio"] = round(
        float(av["available_vehicles"].sum()) / float(insv["vehicles_in_service"].sum()), 4)

    OUT.write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps(rep, indent=2, default=str))


if __name__ == "__main__":
    main()
