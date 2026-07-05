"""Step-1 verification runner: execute the FULL-MEMORY supply pipeline (post
Fix-1 removal) into a SEPARATE output dir, and report real peak working-set memory.

Does not modify any pipeline source. The existing daily baseline under
data/processed/fifth_ring/supply/ is left untouched for comparison.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lib import supply  # noqa: E402

GB = 1024 ** 3
ORDERS = "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
OUT_DIR = "data/processed/fifth_ring/supply_step1_fullmem"


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def peak_gb() -> float:
    c = PMC(); c.cb = ctypes.sizeof(c)
    ctypes.windll.psapi.K32GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.PeakWorkingSetSize / GB


def main() -> None:
    t0 = time.time()
    summary = supply.run_pipeline(
        orders_path=str(PROJECT_ROOT / ORDERS),
        output_dir=str(PROJECT_ROOT / OUT_DIR),
        max_gap_minutes=60,
        carpool_merge_gap_s=0,
        slot_duration_min=15,
        execution_mode="full-memory",
    )
    print(f"\nexecution_mode default in module = {supply.EXECUTION_MODE!r}")
    print(f"peak_working_set_GB = {peak_gb():.2f}")
    print(f"elapsed_min = {(time.time() - t0) / 60:.1f}")
    print("summary:", {k: v for k, v in summary.items() if k != "daily_summaries"})


if __name__ == "__main__":
    main()
