"""Step-1 verification: compare FULL-MEMORY (post Fix-1 removal) supply outputs
against the existing DAILY baseline (with Fix-1), and categorize every difference.

Daily baseline  : data/processed/fifth_ring/supply/            (Fix-1 present)
Full-memory new : data/processed/fifth_ring/supply_step1_fullmem/  (Fix-1 removed)

Read-only; writes a JSON report under outputs/analysis/. Modifies no source.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

DAILY = "data/processed/fifth_ring/supply"
FULL = "data/processed/fifth_ring/supply_step1_fullmem"
OUT = "outputs/analysis/step1_daily_vs_fullmem_report.json"

MIDNIGHT_SLOTS = ["00:00:00", "00:15:00"]
DAYTIME_SLOT = "07:30:00"
EVENING_EDGE = "23:45:00"


def _read(path):
    return pd.read_csv(path)


def _tod(s):
    return pd.to_datetime(s).dt.strftime("%H:%M:%S")


def compare_available_fleet(report):
    da = _read(f"{DAILY}/supply_available_by_cluster.csv.gz")
    fa = _read(f"{FULL}/supply_available_by_cluster.csv.gz")
    df_ = _read(f"{DAILY}/supply_fleet_lower_bound.csv.gz")
    ff = _read(f"{FULL}/supply_fleet_lower_bound.csv.gz")
    for d in (da, fa, df_, ff):
        d["cluster_id"] = d["cluster_id"].astype(str)

    # (2) densification: row counts + nonzero-value equality on DAYTIME slots.
    m = da.merge(fa, on=["slot_start", "cluster_id"], how="outer",
                 suffixes=("_daily", "_full"), indicator=True)
    m["tod"] = _tod(m["slot_start"])
    day_mask = ~m["tod"].isin(MIDNIGHT_SLOTS + [EVENING_EDGE])
    # daytime nonzero cells present in both must have equal value
    both = m[(m["_merge"] == "both") & day_mask]
    dv = both["available_vehicles_daily"].fillna(0).to_numpy()
    fv = both["available_vehicles_full"].fillna(0).to_numpy()
    daytime_mismatch = int(np.count_nonzero(dv != fv))
    # extra rows that exist only in full (densification) -> should be zero-valued
    only_full = m[m["_merge"] == "right_only"]
    extra_full_nonzero = int((only_full["available_vehicles_full"].fillna(0) > 0).sum())
    only_daily = m[m["_merge"] == "left_only"]

    report["available_fleet"] = {
        "daily_available_rows": int(len(da)),
        "full_available_rows": int(len(fa)),
        "row_delta_full_minus_daily": int(len(fa) - len(da)),
        "daily_fleet_rows": int(len(df_)),
        "full_fleet_rows": int(len(ff)),
        "daytime_available_value_mismatches": daytime_mismatch,
        "rows_only_in_full": int(len(only_full)),
        "rows_only_in_full_that_are_NONZERO": extra_full_nonzero,
        "rows_only_in_daily": int(len(only_daily)),
        "rows_only_in_daily_that_are_NONZERO": int((only_daily["available_vehicles_daily"].fillna(0) > 0).sum()),
    }

    # (1) midnight rebate on available
    for tod in MIDNIGHT_SLOTS:
        sub = m[m["tod"] == tod]
        report["available_fleet"][f"available_sum_{tod}_daily"] = int(sub["available_vehicles_daily"].fillna(0).sum())
        report["available_fleet"][f"available_sum_{tod}_full"] = int(sub["available_vehicles_full"].fillna(0).sum())

    # core anchor: 07:30 global_fleet per-day, daily vs full
    def gfb(frame):
        f = frame.copy()
        f["tod"] = _tod(f["slot_start"])
        s = f[f["tod"] == DAYTIME_SLOT].drop_duplicates("slot_start")
        return s["global_fleet_lower_bound"]
    gd, gf = gfb(df_), gfb(ff)
    report["anchor_0730_global_fleet"] = {
        "daily_mean": round(float(gd.mean()), 1), "daily_n_days": int(gd.size),
        "full_mean": round(float(gf.mean()), 1), "full_n_days": int(gf.size),
    }


def compare_in_service(report):
    cols = ["slot_start", "vehicles_in_service"]
    want = set(MIDNIGHT_SLOTS + [DAYTIME_SLOT, EVENING_EDGE])
    acc = {"daily": {t: 0 for t in want}, "full": {t: 0 for t in want}}
    rows = {"daily": 0, "full": 0}
    for key, base in (("daily", DAILY), ("full", FULL)):
        for ch in pd.read_csv(f"{base}/supply_in_service_od.csv.gz", usecols=cols, chunksize=2_000_000):
            rows[key] += len(ch)
            tod = _tod(ch["slot_start"])
            for t in want:
                acc[key][t] += int(ch.loc[tod == t, "vehicles_in_service"].sum())
    report["in_service"] = {
        "daily_rows": rows["daily"], "full_rows": rows["full"],
        "sum_by_tod": {t: {"daily": acc["daily"][t], "full": acc["full"][t],
                           "delta_full_minus_daily": acc["full"][t] - acc["daily"][t]} for t in sorted(want)},
    }


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    report = {}
    # trip_segments count (predicted cross-midnight carpool-merge difference)
    ds = json.load(open(f"{DAILY}/run_summary.json"))
    fs = json.load(open(f"{FULL}/run_summary.json"))
    report["trip_segments_count"] = {
        "daily": ds.get("trip_segments"), "full": fs.get("trip_segments"),
        "delta_full_minus_daily": (fs.get("trip_segments") or 0) - (ds.get("trip_segments") or 0),
    }
    report["full_summary"] = {k: v for k, v in fs.items() if k != "daily_summaries"}
    compare_available_fleet(report)
    compare_in_service(report)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
