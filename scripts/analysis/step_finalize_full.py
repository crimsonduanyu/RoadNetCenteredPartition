"""Post rename+dead-code-deletion: full run via simplified run_pipeline must match
the pre-deletion live-path output (new product names, identical totals, peak ok)."""
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
OUT = ROOT / "outputs/analysis/step_finalize_full_report.json"
GB = 1024 ** 3
# Pre-deletion live-path totals (from step_midnight_full, same compute/threshold).
EXPECT_AVAILABLE_TOTAL = 35_327_426
EXPECT_INSERVICE_TOTAL = 112_773_756
EXPECT_INSERVICE_ROWS = 29_012_070
EXPECT_AVAILABLE_ROWS = 883_200


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
    tmp = Path(tempfile.mkdtemp(prefix="finalize_full_"))
    t0 = time.time()
    summary = supply.run_pipeline(orders_path=str(ORDERS), output_dir=str(tmp),
                                  max_gap_minutes=60, tau_idle_minutes=30,
                                  carpool_merge_gap_s=0, slot_duration_min=15, n_blocks=8)
    peak = peak_gb()

    files = {p.name for p in tmp.glob("*.csv.gz")}
    av = pd.read_csv(tmp / "supply_available_floor.csv.gz", usecols=["available_vehicles"])["available_vehicles"]
    insv = pd.read_csv(tmp / "supply_inservice_od.csv.gz", usecols=["vehicles_in_service"])["vehicles_in_service"]

    rep = {
        "entry": "run_pipeline (simplified, chunked-only)",
        "new_named_files_present": sorted(files),
        "old_named_files_absent": ("supply_available_by_cluster.csv.gz" not in files
                                   and "supply_in_service_od.csv.gz" not in files),
        "memory": {"process_peak_gb": round(peak, 2), "under_waterline_19_2": bool(peak < 19.2)},
        "elapsed_sec": round(time.time() - t0, 1),
        "available_total": int(av.sum()), "available_total_matches_pre_deletion": bool(int(av.sum()) == EXPECT_AVAILABLE_TOTAL),
        "inservice_total": int(insv.sum()), "inservice_total_matches_pre_deletion": bool(int(insv.sum()) == EXPECT_INSERVICE_TOTAL),
        "available_rows": int(summary["available_rows"]), "inservice_rows": int(summary["in_service_rows"]),
        "rows_match_pre_deletion": bool(int(summary["available_rows"]) == EXPECT_AVAILABLE_ROWS
                                        and int(summary["in_service_rows"]) == EXPECT_INSERVICE_ROWS),
    }
    OUT.write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps(rep, indent=2, default=str))


if __name__ == "__main__":
    main()
