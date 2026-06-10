"""Read-only memory/feasibility probe for the planned supply refactor.

Measures REAL peak working-set memory (Windows PeakWorkingSetSize via psapi) for:
  (1) loading the 6 key order columns into a DataFrame,
  (2) full all-driver chain/block/gap reconstruction (R1 merge + busy blocks +
      cross-driver sort + inter-block gaps),
  (3) a 10% driver subsample reconstruction, linearly extrapolated as a cross-check,
and reports current free physical RAM + a clear "full vs per-driver-chunk" verdict.

Reads only the supply input file; does NOT import or run any pipeline code and
does NOT modify anything.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import gc
import json
import os
import time

import numpy as np
import pandas as pd

ORDERS_PATH = "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
OUT_JSON = "outputs/analysis/supply_mem_probe_report.json"
USECOLS = ["driver_id", "departure_time", "finish_time",
           "origin_cluster_id", "destination_cluster_id", "service_type"]
TIME_FMT = "%Y-%m-%d %H:%M:%S"
CHUNK = 5_000_000
SAFETY_FRACTION = 0.60  # "full feasible" if full peak < 60% of currently-free RAM
GB = 1024 ** 3


# ----- Windows memory APIs (peak working set + available physical RAM) -----
class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


_K32 = ctypes.windll.kernel32
_K32.GetCurrentProcess.restype = ctypes.c_void_p
_GET_PMI = _K32.K32GetProcessMemoryInfo  # exported from kernel32 on Win7+
_GET_PMI.restype = wintypes.BOOL
_GET_PMI.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]


def mem_now() -> tuple[float, float]:
    """(current_working_set_GB, peak_working_set_GB) for this process."""
    c = PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(c)
    ok = _GET_PMI(_K32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    if not ok:
        raise OSError(f"K32GetProcessMemoryInfo failed (err={ctypes.get_last_error()})")
    return c.WorkingSetSize / GB, c.PeakWorkingSetSize / GB


def phys_mem() -> tuple[float, float]:
    """(total_phys_GB, avail_phys_GB)."""
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullTotalPhys / GB, m.ullAvailPhys / GB


def log(msg: str) -> None:
    cur, peak = mem_now()
    print(f"[{time.strftime('%H:%M:%S')}] RSS={cur:6.2f}GB peak={peak:6.2f}GB | {msg}", flush=True)


# ----------------------- load 6 key columns -----------------------
def load_columns(path: str) -> pd.DataFrame:
    parts = []
    n_raw = 0
    for i, ch in enumerate(pd.read_csv(path, usecols=USECOLS, chunksize=CHUNK)):
        n_raw += len(ch)
        dep = pd.to_datetime(ch["departure_time"], format=TIME_FMT, errors="coerce")
        fin = pd.to_datetime(ch["finish_time"], format=TIME_FMT, errors="coerce")
        sub = pd.DataFrame(
            {
                "driver_id": ch["driver_id"].to_numpy(dtype="int64"),
                "start_ns": dep.values.astype("int64"),
                "finish_ns": fin.values.astype("int64"),
                "origin": ch["origin_cluster_id"].to_numpy(dtype="int16"),
                "dest": ch["destination_cluster_id"].to_numpy(dtype="int16"),
                "is_carpool": (ch["service_type"].to_numpy() == "carpool"),
            }
        )
        parts.append(sub)
    df = pd.concat(parts, ignore_index=True)
    del parts
    gc.collect()
    df.attrs["n_raw_rows"] = n_raw
    return df


# ------------- reconstruction (R1 merge + blocks + gaps) -------------
def reconstruct(df: pd.DataFrame) -> dict:
    cp = df[df["is_carpool"]].sort_values(["driver_id", "start_ns", "finish_ns"], kind="mergesort").reset_index(drop=True)
    if len(cp):
        run_end = cp.groupby("driver_id")["finish_ns"].cummax()
        prev_run = run_end.groupby(cp["driver_id"]).shift()
        new_grp = prev_run.isna() | (cp["start_ns"] >= prev_run)
        grp = new_grp.groupby(cp["driver_id"]).cumsum().astype("int64")
        cp_blocks = (
            cp.assign(_g=grp)
            .groupby(["driver_id", "_g"], sort=False)
            .agg(ts=("start_ns", "min"), te=("finish_ns", "max"),
                 origin=("origin", "first"), dest=("dest", "last"))
            .reset_index()[["driver_id", "ts", "te", "origin", "dest"]]
        )
    else:
        cp_blocks = pd.DataFrame(columns=["driver_id", "ts", "te", "origin", "dest"])
    ex = df[~df["is_carpool"]]
    ex_blocks = pd.DataFrame(
        {"driver_id": ex["driver_id"].to_numpy(), "ts": ex["start_ns"].to_numpy(),
         "te": ex["finish_ns"].to_numpy(), "origin": ex["origin"].to_numpy(), "dest": ex["dest"].to_numpy()}
    )
    blocks = pd.concat([cp_blocks, ex_blocks], ignore_index=True)
    blocks = blocks.sort_values(["driver_id", "ts", "te"], kind="mergesort").reset_index(drop=True)

    drv = blocks["driver_id"].to_numpy()
    ts = blocks["ts"].to_numpy()
    te = blocks["te"].to_numpy()
    has_next = np.zeros(len(blocks), dtype=bool)
    has_next[:-1] = drv[:-1] == drv[1:]
    nxt = np.empty(len(blocks), dtype=ts.dtype)
    nxt[:-1] = ts[1:]
    gap_min = (nxt[has_next] - te[has_next]) / 60_000_000_000
    return {"n_blocks": int(len(blocks)), "n_gaps": int(has_next.sum()),
            "gap_mean_min": float(gap_min.mean()) if gap_min.size else float("nan")}


def main() -> None:
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    t0 = time.time()
    total_gb, avail_start_gb = phys_mem()
    log(f"start. phys total={total_gb:.1f}GB avail={avail_start_gb:.1f}GB")

    # (1) load
    df = load_columns(ORDERS_PATH)
    n_rows = len(df)
    deep_gb = float(df.memory_usage(deep=True).sum()) / GB
    cur_after_load, peak_after_load = mem_now()
    log(f"loaded {n_rows:,} rows. df.memory_usage(deep)={deep_gb:.2f}GB")

    # (3-subsample, run BEFORE full so its cumulative peak excludes full's allocs)
    drivers = df["driver_id"].drop_duplicates()
    n_drivers = int(drivers.size)
    sample_drivers = set(drivers.sample(frac=0.10, random_state=0).tolist())
    sub_df = df[df["driver_id"].isin(sample_drivers)].copy()
    sub_rows = len(sub_df)
    _, peak_before_sub = mem_now()
    sub_res = reconstruct(sub_df)
    _, peak_after_sub = mem_now()
    sub_overhead = peak_after_sub - peak_before_sub
    del sub_df
    gc.collect()
    log(f"subsample 10% drivers: rows={sub_rows:,} overhead~{sub_overhead:.2f}GB blocks={sub_res['n_blocks']:,}")

    # (2) full reconstruction
    _, peak_before_full = mem_now()
    full_res = reconstruct(df)
    cur_after_full, peak_after_full = mem_now()
    full_overhead = peak_after_full - peak_before_full
    _, avail_now_gb = phys_mem()

    extrap_full_overhead = sub_overhead * (n_rows / sub_rows) if sub_rows else float("nan")

    # Verdict: compare absolute full peak working set against safety waterline.
    safety_gb = avail_start_gb * SAFETY_FRACTION
    full_feasible = peak_after_full < safety_gb

    report = {
        "orders_path": ORDERS_PATH,
        "phys_total_gb": round(total_gb, 1),
        "phys_avail_at_start_gb": round(avail_start_gb, 1),
        "phys_avail_after_full_gb": round(avail_now_gb, 1),
        "n_rows_decompressed": int(n_rows),
        "n_raw_rows": int(df.attrs.get("n_raw_rows", n_rows)),
        "n_drivers": n_drivers,
        "load": {
            "df_memory_usage_deep_gb": round(deep_gb, 2),
            "peak_working_set_after_load_gb": round(peak_after_load, 2),
        },
        "full_reconstruction": {
            "n_busy_blocks": full_res["n_blocks"],
            "n_inter_block_gaps": full_res["n_gaps"],
            "reconstruction_overhead_gb": round(full_overhead, 2),
            "process_peak_working_set_gb": round(peak_after_full, 2),
        },
        "subsample_10pct_drivers": {
            "rows": sub_rows,
            "measured_overhead_gb": round(sub_overhead, 2),
            "linear_extrapolated_full_overhead_gb": round(extrap_full_overhead, 2),
            "extrapolation_assumption": "peak overhead scales linearly with row count; "
            "groupby/sort are ~O(n log n) so this slightly UNDER-estimates, treated as a floor.",
        },
        "verdict": {
            "safety_fraction": SAFETY_FRACTION,
            "safety_waterline_gb": round(safety_gb, 1),
            "full_process_peak_gb": round(peak_after_full, 2),
            "full_feasible_under_waterline": bool(full_feasible),
            "decision": ("DROP BUCKETING -> FULL IN-MEMORY" if full_feasible
                         else "KEEP CHUNKING, BUT BY DRIVER (not by day)"),
        },
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    log(f"wrote {OUT_JSON}")
    print("\n================ SUPPLY MEMORY PROBE ================")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
