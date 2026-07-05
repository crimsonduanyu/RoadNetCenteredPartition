"""Full-data driver-chunked run: real peak memory + full-scale anchors (read-only temp out)."""
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
OUT = ROOT / "outputs/analysis/step_chunk_full_report.json"
GB = 1024 ** 3
WATERLINE = 19.2


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
    tmp = Path(tempfile.mkdtemp(prefix="chunk_full_"))
    t0 = time.time()
    summary = supply.run_chunked_pipeline(orders_path=str(ORDERS), output_dir=str(tmp),
                                          max_gap_minutes=60, carpool_merge_gap_s=0, slot_duration_min=15,
                                          n_blocks=supply.DRIVER_BLOCKS)
    peak = peak_gb()
    elapsed = round(time.time() - t0, 1)

    # 07:30 anchor across all days: chunked NEW vs daily-OLD (expect NEW>=OLD; no daytime regression).
    c_fl = pd.read_csv(tmp / "supply_fleet_lower_bound.csv.gz", usecols=["slot_start", "global_fleet_lower_bound"])
    c_fl["slot_start"] = pd.to_datetime(c_fl["slot_start"])
    o_fl = pd.read_csv(DAILY / "supply_fleet_lower_bound.csv.gz", usecols=["slot_start", "global_fleet_lower_bound"])
    o_fl["slot_start"] = pd.to_datetime(o_fl["slot_start"])
    cg = c_fl.drop_duplicates("slot_start").set_index("slot_start")["global_fleet_lower_bound"]
    og = o_fl.drop_duplicates("slot_start").set_index("slot_start")["global_fleet_lower_bound"]
    common = cg.index.intersection(og.index)
    is0730 = common[common.strftime("%H:%M:%S") == "07:30:00"]
    d = (cg.loc[is0730] - og.loc[is0730])
    rep = {
        "execution_summary": {k: v for k, v in summary.items() if k != "block_summaries"},
        "block_summaries": summary["block_summaries"],
        "memory": {"process_peak_gb": round(peak, 2), "waterline_gb": WATERLINE,
                   "under_waterline": bool(peak < WATERLINE), "margin_gb": round(WATERLINE - peak, 2)},
        "elapsed_sec": elapsed,
        "anchor_0730_global_fleet": {
            "days_compared": int(len(is0730)),
            "new_ge_old_all_days": bool((d >= 0).all()),
            "new_lt_old_days": int((d < 0).sum()),
            "mean_new": round(float(cg.loc[is0730].mean()), 1),
            "mean_old": round(float(og.loc[is0730].mean()), 1),
            "mean_delta_new_minus_old": round(float(d.mean()), 2),
            "max_delta": int(d.max()), "min_delta": int(d.min()),
        },
    }
    OUT.write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps({k: v for k, v in rep.items() if k != "block_summaries"}, indent=2, default=str))


if __name__ == "__main__":
    main()
