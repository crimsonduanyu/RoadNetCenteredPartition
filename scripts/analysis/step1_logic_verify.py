"""Step-1 BOUNDED logic verification (read-only; no pipeline edits).

Proves the correctness of "switch to full-memory + delete Fix-1" on a small
4-day slice, where correctness is independent of the (separate) memory problem.

Compares, on the INTERIOR days of the slice:
  full-memory-NEW : current process_orders_frame() over the whole slice at once
                    (Fix-1 deleted) -- computed live here.
  daily-OLD       : the existing full-data daily baseline under
                    data/processed/fifth_ring/supply/ (generated WITH Fix-1),
                    filtered to the interior-day slots.

Evidence collected:
  (1) cross-midnight rebate: full-memory NEW emits the 00:00/00:15 in-service
      that Fix-1 used to clip; sample a cross-midnight carpool segment and show
      its post-midnight slots + correct OD label.
  (2) densification = zero-padding only: NEW available/fleet grid is dense
      (global clusters x continuous slots); daytime nonzero values match OLD,
      extra NEW rows are all zero.
  (3) daytime anchor (07:30) identical NEW vs OLD -> deleting Fix-1 did not
      touch daytime.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SUPPLY_PY = ROOT / "src" / "lib" / "supply.py"
ORDERS = ROOT / "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
DAILY = ROOT / "data/processed/fifth_ring/supply"
OUT = ROOT / "outputs/analysis/step1_logic_verify_report.json"

USECOLS = ["order_id", "driver_id", "departure_time", "finish_time",
           "origin_cluster_id", "destination_cluster_id", "service_type"]
MAXGAP, CARPOOL_GAP, SLOT = 60, 0, 15


def load_supply():
    spec = importlib.util.spec_from_file_location("supply_live", SUPPLY_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_slice(days: set[str]) -> pd.DataFrame:
    parts = []
    for ch in pd.read_csv(ORDERS, usecols=USECOLS, chunksize=2_000_000):
        dep = pd.to_datetime(ch["departure_time"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
        keep = dep.dt.strftime("%Y-%m-%d").isin(days)
        if keep.any():
            sub = ch.loc[keep].copy()
            sub["departure_time"] = dep.loc[keep]
            sub["finish_time"] = pd.to_datetime(sub["finish_time"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
            parts.append(sub)
    return pd.concat(parts, ignore_index=True)


def tod(s):
    return pd.to_datetime(s).dt.strftime("%H:%M:%S")


def date_str(s):
    return pd.to_datetime(s).dt.strftime("%Y-%m-%d")


def main():
    os.makedirs(OUT.parent, exist_ok=True)
    supply = load_supply()
    report = {"execution_mode_default_now": supply.EXECUTION_MODE}

    ds = json.load(open(DAILY / "run_summary.json"))["daily_summaries"]
    slice_days = sorted(d["date"] for d in ds)[:4]
    interior = set(slice_days[1:-1])  # exclude first/last to avoid slice-edge effects
    report["slice_days"] = slice_days
    report["interior_days"] = sorted(interior)

    orders = read_slice(set(slice_days))
    report["slice_orders"] = int(len(orders))

    # ---- full-memory NEW (live, Fix-1 deleted) ----
    out = supply.process_orders_frame(orders, MAXGAP, CARPOOL_GAP, SLOT)
    new_in = out["supply_in_service_od"].copy()
    new_av = out["supply_available_by_cluster"].copy()
    new_fl = out["supply_fleet_lower_bound"].copy()
    for d in (new_in, new_av, new_fl):
        d["slot_start"] = pd.to_datetime(d["slot_start"])
    seg_all = out["trip_segments"]

    # ---- daily OLD baseline (Fix-1 ON), filtered to interior slots ----
    def read_daily(name):
        d = pd.read_csv(DAILY / name)
        d["slot_start"] = pd.to_datetime(d["slot_start"])
        return d[date_str(d["slot_start"]).isin(interior)].copy()
    old_in = read_daily("supply_in_service_od.csv.gz")
    old_av = read_daily("supply_available_by_cluster.csv.gz")
    old_fl = read_daily("supply_fleet_lower_bound.csv.gz")

    new_in_i = new_in[date_str(new_in["slot_start"]).isin(interior)]
    new_av_i = new_av[date_str(new_av["slot_start"]).isin(interior)]
    new_fl_i = new_fl[date_str(new_fl["slot_start"]).isin(interior)]

    # === (3) daytime 07:30 anchor: NEW vs OLD must match ===
    def at(frame, t, col):
        return int(frame.loc[tod(frame["slot_start"]) == t, col].sum())
    anchor = {}
    for day in sorted(interior):
        slot = f"{day} 07:30:00"
        no = int(new_fl_i.loc[new_fl_i["slot_start"] == slot, "global_fleet_lower_bound"].drop_duplicates().sum())
        oo = int(old_fl.loc[old_fl["slot_start"] == slot, "global_fleet_lower_bound"].drop_duplicates().sum())
        ni = int(new_in_i.loc[new_in_i["slot_start"] == slot, "vehicles_in_service"].sum())
        oi = int(old_in.loc[old_in["slot_start"] == slot, "vehicles_in_service"].sum())
        # Explain any NEW-vs-OLD gap: distinct drivers in-service at 07:30 whose trip
        # STARTED on an earlier day (long cross-midnight trips Fix-1 had clipped).
        slot_ts = pd.Timestamp(slot)
        slot_end = slot_ts + pd.Timedelta(minutes=SLOT)
        live = seg_all[(pd.to_datetime(seg_all["trip_start"]) < slot_end)
                       & (pd.to_datetime(seg_all["trip_end"]) > slot_ts)]
        prev = live[pd.to_datetime(live["trip_start"]).dt.strftime("%Y-%m-%d") < day]
        carryover_drivers = int(prev["driver_id"].nunique())
        anchor[day] = {"global_fleet_new": no, "global_fleet_old": oo, "fleet_delta_new_minus_old": no - oo,
                       "in_service_sum_new": ni, "in_service_sum_old": oi, "in_service_delta_new_minus_old": ni - oi,
                       "crossmidnight_carryover_drivers_in_service_at_0730": carryover_drivers}
    report["(3)_daytime_0730_anchor"] = anchor

    # === (1) midnight rebate: NEW vs OLD in-service sum at 00:00 / 00:15 ===
    rebate = {}
    for day in sorted(interior):
        for hhmm in ("00:00:00", "00:15:00"):
            slot = f"{day} {hhmm}"
            ni = int(new_in_i.loc[new_in_i["slot_start"] == slot, "vehicles_in_service"].sum())
            oi = int(old_in.loc[old_in["slot_start"] == slot, "vehicles_in_service"].sum())
            rebate[slot] = {"in_service_new": ni, "in_service_old": oi, "rebate_new_minus_old": ni - oi}
    report["(1)_midnight_rebate"] = rebate

    # (1b) sample cross-midnight CARPOOL segment + show its post-midnight slots/OD
    seg = out["trip_segments"].copy()
    seg["d0"] = pd.to_datetime(seg["trip_start"]).dt.strftime("%Y-%m-%d")
    seg["d1"] = pd.to_datetime(seg["trip_end"]).dt.strftime("%Y-%m-%d")
    xmid = seg[(seg["d0"] != seg["d1"]) & (seg["service_type"] == "carpool") & seg["d0"].isin({slice_days[0], *interior})]
    sample = {}
    if not xmid.empty:
        row = xmid.iloc[0]
        one = pd.DataFrame([{"driver_id": row["driver_id"], "trip_start": row["trip_start"],
                             "trip_end": row["trip_end"], "origin_cluster_id": row["origin_cluster_id"],
                             "destination_cluster_id": row["destination_cluster_id"]}])
        _, dsl = supply.compute_in_service_od(one, slot_duration_min=SLOT, return_driver_slots=True)
        sample = {
            "segment_id": str(row["segment_id"]),
            "trip_start": str(row["trip_start"]), "trip_end": str(row["trip_end"]),
            "origin_cluster_id": int(row["origin_cluster_id"]), "destination_cluster_id": int(row["destination_cluster_id"]),
            "expanded_slots": sorted(dsl["slot_start"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist()),
            "n_xmidnight_carpool_segments_in_slice": int(len(xmid)),
        }
    report["(1b)_crossmidnight_carpool_sample"] = sample

    # === (2) densification = zero padding only ===
    # Use a TRUE daytime band [06:00,22:00) so the <=60min midnight idle-rebate band
    # (00:00-00:45, 23:15-23:59) cannot contaminate the nonzero-equality check.
    def is_daytime(frame):
        hh = pd.to_datetime(frame["slot_start"]).dt.hour
        return (hh >= 6) & (hh < 22)
    old_av_day = old_av[is_daytime(old_av)]
    new_av_day = new_av_i[is_daytime(new_av_i)]
    m = old_av_day.merge(new_av_day, on=["slot_start", "cluster_id"], how="outer",
                         suffixes=("_old", "_new"), indicator=True)
    both = m[m["_merge"] == "both"]
    nz_mismatch = int(((both["available_vehicles_old"].fillna(0)) != (both["available_vehicles_new"].fillna(0))).sum())
    only_new = m[m["_merge"] == "right_only"]
    only_new_nonzero = int((only_new["available_vehicles_new"].fillna(0) > 0).sum())
    report["(2)_densification"] = {
        "old_available_rows_interior": int(len(old_av)),
        "new_available_rows_interior": int(len(new_av_i)),
        "daytime_nonzero_value_mismatches": nz_mismatch,
        "rows_only_in_new_daytime": int(len(only_new)),
        "rows_only_in_new_that_are_NONZERO": only_new_nonzero,
    }

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False, default=str)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
