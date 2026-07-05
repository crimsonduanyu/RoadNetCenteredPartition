"""Verify midnight-break removal: A unchanged, daytime unchanged, midnight rebate.

Compares NEW (current code, no midnight break) vs OLD (midnight break re-added via
a local monkeypatch -- source unchanged) on a 4-day slice, both at tau_idle=30,
max_gap=60. In-service (A) depends only on trip_segments (not chains), so it must
be IDENTICAL everywhere; available/fleet change only at midnight-adjacent slots
where a short cross-midnight gap now forms one chain.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SUPPLY_PY = ROOT / "src" / "lib" / "supply.py"
ORDERS = ROOT / "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
DAILY = ROOT / "data/processed/fifth_ring/supply"
OUT = ROOT / "outputs/analysis/step_midnight_verify_report.json"
USECOLS = ["order_id", "driver_id", "departure_time", "finish_time",
           "origin_cluster_id", "destination_cluster_id", "service_type"]
FMT = "%Y-%m-%d %H:%M:%S"


def load_supply():
    spec = importlib.util.spec_from_file_location("supply_live", SUPPLY_PY)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def make_old_reconstruct(supply):
    """OLD reconstruct WITH the midnight break (date_ != prev_date), for comparison."""
    def old_reconstruct(trip_segments, max_gap_minutes=supply.MAX_GAP_MINUTES):
        if trip_segments.empty:
            return trip_segments.assign(chain_id=pd.Series(dtype="object")), pd.DataFrame()
        segments = trip_segments.sort_values(
            ["driver_id", "trip_start", "trip_end", "segment_id"], kind="mergesort").reset_index(drop=True)
        segments["date_"] = segments["trip_start"].dt.date.astype(str)
        prev_end = segments.groupby("driver_id")["trip_end"].shift()
        prev_date = segments.groupby("driver_id")["date_"].shift()
        gap_min = (segments["trip_start"] - prev_end).dt.total_seconds().div(60)
        new_chain = prev_end.isna() | (gap_min > max_gap_minutes) | segments["date_"].ne(prev_date)
        segments["chain_seq"] = new_chain.groupby([segments["driver_id"], segments["date_"]]).cumsum().astype("int64")
        segments["chain_id"] = (segments["driver_id"].astype(str) + "_" + segments["date_"].astype(str)
                                + "_" + segments["chain_seq"].astype(str))
        return segments, pd.DataFrame()
    return old_reconstruct


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
    rep = {}
    days = sorted(d["date"] for d in json.load(open(DAILY / "run_summary.json"))["daily_summaries"])[:4]
    interior = set(days[1:-1])
    orders = read_slice(set(days))
    rep["slice_days"], rep["slice_orders"] = days, int(len(orders))

    # NEW (current, no midnight break), then OLD (monkeypatched midnight break).
    new = supply.process_orders_frame(orders, 60, 0, 15, tau_idle_minutes=30)
    supply.reconstruct_driver_chains = make_old_reconstruct(supply)
    old = supply.process_orders_frame(orders, 60, 0, 15, tau_idle_minutes=30)

    def keyed(df, keys, val):
        d = df.copy(); d["slot_start"] = pd.to_datetime(d["slot_start"])
        return d.set_index(keys)[val]

    # (1) A in-service identical EVERYWHERE (in-service is independent of chains).
    an = keyed(new["supply_in_service_od"], ["slot_start", "origin_cluster_id", "destination_cluster_id"], "vehicles_in_service")
    ao = keyed(old["supply_in_service_od"], ["slot_start", "origin_cluster_id", "destination_cluster_id"], "vehicles_in_service")
    aj = pd.concat([an.rename("n"), ao.rename("o")], axis=1).fillna(0)
    rep["(1)_in_service_A_identical_everywhere"] = {
        "identical": bool((aj["n"] == aj["o"]).all()), "cells_differing": int((aj["n"] != aj["o"]).sum())}

    # (2) available: split daytime vs midnight band.
    bn = keyed(new["supply_available_by_cluster"], ["slot_start", "cluster_id"], "available_vehicles")
    bo = keyed(old["supply_available_by_cluster"], ["slot_start", "cluster_id"], "available_vehicles")
    bj = pd.concat([bn.rename("n"), bo.rename("o")], axis=1).fillna(0).reset_index()
    hh = pd.to_datetime(bj["slot_start"]).dt.hour
    daytime = (hh >= 6) & (hh < 22)
    midnight = hh.isin([0])  # 00:00-00:45 band (idle <= tau_idle=30 -> only 00:00/00:15 matter)
    rep["(2)_available"] = {
        "daytime_nonidentical_cells": int((bj.loc[daytime, "n"] != bj.loc[daytime, "o"]).sum()),  # MUST be 0
        "daytime_new_lt_old": int((bj.loc[daytime, "n"] < bj.loc[daytime, "o"]).sum()),
        "midnight_new_lt_old": int((bj.loc[midnight, "n"] < bj.loc[midnight, "o"]).sum()),  # MUST be 0
        "midnight_new_ge_old": bool((bj.loc[midnight, "n"] >= bj.loc[midnight, "o"]).all()),
        "available_total_new": int(bj["n"].sum()), "available_total_old": int(bj["o"].sum()),
        "recovered_available": int(bj["n"].sum() - bj["o"].sum()),
        "recovered_pct_of_old": round(100.0 * (bj["n"].sum() - bj["o"].sum()) / bj["o"].sum(), 3) if bj["o"].sum() else None,
    }

    # (2b) cross-midnight idle recovered: idle windows present in NEW, absent in OLD,
    # whose window straddles a midnight boundary.
    def xmid_idle(idle):
        i = idle.copy()
        i["d0"] = pd.to_datetime(i["idle_start"]).dt.strftime("%Y-%m-%d")
        i["d1"] = pd.to_datetime(i["idle_end"]).dt.strftime("%Y-%m-%d")
        return i[i["d0"] != i["d1"]]
    xn, xo = xmid_idle(new["idle_windows"]), xmid_idle(old["idle_windows"])
    sample = xn.head(1)
    rep["(2b)_crossmidnight_idle"] = {
        "xmidnight_idle_windows_new": int(len(xn)), "xmidnight_idle_windows_old": int(len(xo)),
        "recovered_xmidnight_idle_windows": int(len(xn) - len(xo)),
        "sample": ({"driver_id": int(sample.iloc[0]["driver_id"]),
                    "idle_start": str(sample.iloc[0]["idle_start"]), "idle_end": str(sample.iloc[0]["idle_end"]),
                    "idle_duration_min": round(float(sample.iloc[0]["idle_duration_s"]) / 60.0, 1),
                    "idle_cluster_id": int(sample.iloc[0]["idle_cluster_id"])} if len(sample) else None),
    }

    # (4) fleet global daytime unchanged.
    gn = new["supply_fleet_lower_bound"][["slot_start", "global_fleet_lower_bound"]].drop_duplicates()
    go = old["supply_fleet_lower_bound"][["slot_start", "global_fleet_lower_bound"]].drop_duplicates()
    gn["slot_start"] = pd.to_datetime(gn["slot_start"]); go["slot_start"] = pd.to_datetime(go["slot_start"])
    gj = gn.merge(go, on="slot_start", suffixes=("_n", "_o"))
    gj_day = gj[(gj["slot_start"].dt.hour >= 6) & (gj["slot_start"].dt.hour < 22)]
    rep["(4)_fleet_global"] = {
        "daytime_nonidentical": int((gj_day["global_fleet_lower_bound_n"] != gj_day["global_fleet_lower_bound_o"]).sum()),
        "any_new_lt_old": int((gj["global_fleet_lower_bound_n"] < gj["global_fleet_lower_bound_o"]).sum()),
    }

    OUT.write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps(rep, indent=2, default=str))


if __name__ == "__main__":
    main()
