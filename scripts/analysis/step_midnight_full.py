"""Full-data run after midnight-break removal: memory peak + full recovered available + 07:30 anchor."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import importlib.util
import json
import tempfile
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SUPPLY_PY = ROOT / "src" / "lib" / "supply.py"
ORDERS = ROOT / "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
DAILY = ROOT / "data/processed/fifth_ring/supply"
OUT = ROOT / "outputs/analysis/step_midnight_full_report.json"
GB = 1024 ** 3
# OLD (midnight-break present) full available total at tau_idle=30, from the prior step.
OLD_TAU30_AVAILABLE_TOTAL = 34_806_336


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


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    supply = load_supply()
    tmp = Path(tempfile.mkdtemp(prefix="midnight_full_"))
    t0 = time.time()
    summary = supply.run_chunked_pipeline(orders_path=str(ORDERS), output_dir=str(tmp),
                                          max_gap_minutes=60, carpool_merge_gap_s=0, slot_duration_min=15,
                                          n_blocks=supply.DRIVER_BLOCKS, tau_idle_minutes=30)
    peak = peak_gb()
    av = pd.read_csv(tmp / "supply_available_by_cluster.csv.gz", usecols=["available_vehicles"])["available_vehicles"].sum()
    insv = pd.read_csv(tmp / "supply_in_service_od.csv.gz", usecols=["vehicles_in_service"])["vehicles_in_service"].sum()

    # 07:30 daytime anchor vs daily-OLD (midnight removal must not touch daytime).
    c_fl = pd.read_csv(tmp / "supply_fleet_lower_bound.csv.gz", usecols=["slot_start", "global_fleet_lower_bound"])
    c_fl["slot_start"] = pd.to_datetime(c_fl["slot_start"])
    o_fl = pd.read_csv(DAILY / "supply_fleet_lower_bound.csv.gz", usecols=["slot_start", "global_fleet_lower_bound"])
    o_fl["slot_start"] = pd.to_datetime(o_fl["slot_start"])
    cg = c_fl.drop_duplicates("slot_start").set_index("slot_start")["global_fleet_lower_bound"]
    og = o_fl.drop_duplicates("slot_start").set_index("slot_start")["global_fleet_lower_bound"]
    common = cg.index.intersection(og.index)
    is0730 = common[common.strftime("%H:%M:%S") == "07:30:00"]
    d = cg.loc[is0730] - og.loc[is0730]

    rep = {
        "execution_summary": {k: v for k, v in summary.items() if k != "block_summaries"},
        "memory": {"process_peak_gb": round(peak, 2), "under_waterline_19_2": bool(peak < 19.2),
                   "margin_gb": round(19.2 - peak, 2)},
        "elapsed_sec": round(time.time() - t0, 1),
        "available_total_new": int(av),
        "available_total_old_tau30_with_midnight_break": OLD_TAU30_AVAILABLE_TOTAL,
        "recovered_available_full": int(av - OLD_TAU30_AVAILABLE_TOTAL),
        "recovered_pct_full": round(100.0 * (av - OLD_TAU30_AVAILABLE_TOTAL) / OLD_TAU30_AVAILABLE_TOTAL, 3),
        "in_service_total": int(insv),
        "anchor_0730_vs_dailyOLD": {
            "days": int(len(is0730)), "new_ge_old_all_days": bool((d >= 0).all()),
            "new_lt_old_days": int((d < 0).sum()), "mean_delta": round(float(d.mean()), 2)},
    }
    OUT.write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps(rep, indent=2, default=str))


if __name__ == "__main__":
    main()
