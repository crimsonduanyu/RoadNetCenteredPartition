"""Slice-level verification of the driver-chunked skeleton (read-only, temp output).

PRIMARY test  : chunked(8 blocks) == whole-frame process_orders_frame, EXACTLY,
                on every (slot, key) cell. Both run the same Fix-1-removed compute,
                so any mismatch == a chunking bug (invariant 1-4 violation).
SECONDARY test: chunked vs daily-OLD baseline (Fix-1 ON) on interior days ->
                daytime NEW>=OLD (the known long-cross-midnight carryover from
                step-1), midnight ① rebate. NEW<OLD anywhere = bug.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SUPPLY_PY = ROOT / "src" / "lib" / "supply.py"
ORDERS = ROOT / "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
DAILY = ROOT / "data/processed/fifth_ring/supply"
OUT = ROOT / "outputs/analysis/step_chunk_verify_report.json"
USECOLS = ["order_id", "driver_id", "departure_time", "finish_time",
           "origin_cluster_id", "destination_cluster_id", "service_type"]
FMT = "%Y-%m-%d %H:%M:%S"


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


def ds(s):
    return pd.to_datetime(s).dt.strftime("%Y-%m-%d")


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    supply = load_supply()
    rep = {"default_execution_mode": supply.EXECUTION_MODE}

    days = sorted(d["date"] for d in json.load(open(DAILY / "run_summary.json"))["daily_summaries"])[:4]
    interior = set(days[1:-1])
    rep["slice_days"], rep["interior_days"] = days, sorted(interior)

    orders = read_slice(set(days))
    rep["slice_orders"] = int(len(orders))

    # ---- run chunked path on the slice (writes temp products; invariants asserted inside) ----
    tmp = Path(tempfile.mkdtemp(prefix="chunk_verify_"))
    slice_csv = tmp / "slice_orders.csv.gz"
    orders[USECOLS].to_csv(slice_csv, index=False, compression="gzip")
    out_dir = tmp / "chunked_out"
    chunk_summary = supply.run_chunked_pipeline(orders_path=str(slice_csv), output_dir=str(out_dir),
                                                max_gap_minutes=60, carpool_merge_gap_s=0, slot_duration_min=15,
                                                n_blocks=supply.DRIVER_BLOCKS)
    rep["chunk_summary"] = {k: v for k, v in chunk_summary.items() if k != "block_summaries"}
    rep["invariants_1_4_assertions"] = "passed (no AssertionError raised during chunked run)"

    def read_out(name):
        d = pd.read_csv(out_dir / name); d["slot_start"] = pd.to_datetime(d["slot_start"]); return d
    c_in = read_out("supply_in_service_od.csv.gz")
    c_av = read_out("supply_available_by_cluster.csv.gz")
    c_fl = read_out("supply_fleet_lower_bound.csv.gz")

    # ---- whole-frame NEW (same Fix-1-removed compute, one frame) ----
    wf = supply.process_orders_frame(orders, 60, 0, 15)
    w_in = wf["supply_in_service_od"].copy(); w_in["slot_start"] = pd.to_datetime(w_in["slot_start"])
    w_av = wf["supply_available_by_cluster"].copy(); w_av["slot_start"] = pd.to_datetime(w_av["slot_start"])
    w_fl = wf["supply_fleet_lower_bound"].copy(); w_fl["slot_start"] = pd.to_datetime(w_fl["slot_start"])

    # === PRIMARY: chunked == whole-frame exactly ===
    def cmp_keys(a, b, keys, val):
        m = a.merge(b, on=keys, how="outer", suffixes=("_c", "_w"), indicator=True)
        va, vb = m[f"{val}_c"].fillna(0), m[f"{val}_w"].fillna(0)
        mism = m[va != vb]
        return {"rows_chunk": int(len(a)), "rows_wholeframe": int(len(b)),
                "only_in_chunk": int((m["_merge"] == "left_only").sum()),
                "only_in_wholeframe": int((m["_merge"] == "right_only").sum()),
                "value_mismatches": int(len(mism)),
                "max_abs_diff": int((va - vb).abs().max()) if len(m) else 0}
    rep["PRIMARY_chunked_vs_wholeframe"] = {
        "in_service": cmp_keys(c_in, w_in, ["slot_start", "origin_cluster_id", "destination_cluster_id"], "vehicles_in_service"),
        "available": cmp_keys(c_av, w_av, ["slot_start", "cluster_id"], "available_vehicles"),
        "fleet_cluster": cmp_keys(c_fl[["slot_start", "cluster_id", "fleet_lower_bound_cluster"]],
                                  w_fl[["slot_start", "cluster_id", "fleet_lower_bound_cluster"]],
                                  ["slot_start", "cluster_id"], "fleet_lower_bound_cluster"),
    }
    # global fleet per slot exact
    cg = c_fl[["slot_start", "global_fleet_lower_bound"]].drop_duplicates()
    wg = w_fl[["slot_start", "global_fleet_lower_bound"]].drop_duplicates()
    gm = cg.merge(wg, on="slot_start", how="outer", suffixes=("_c", "_w"))
    rep["PRIMARY_chunked_vs_wholeframe"]["global_fleet_mismatches"] = int(
        (gm["global_fleet_lower_bound_c"].fillna(0) != gm["global_fleet_lower_bound_w"].fillna(0)).sum())

    # === SECONDARY: chunked vs daily-OLD (interior) ===
    def read_daily(name):
        d = pd.read_csv(DAILY / name); d["slot_start"] = pd.to_datetime(d["slot_start"])
        return d[ds(d["slot_start"]).isin(interior)]
    o_in = read_daily("supply_in_service_od.csv.gz")
    o_fl = read_daily("supply_fleet_lower_bound.csv.gz")
    c_in_i = c_in[ds(c_in["slot_start"]).isin(interior)]
    c_fl_i = c_fl[ds(c_fl["slot_start"]).isin(interior)]
    sec = {}
    for day in sorted(interior):
        d0730 = f"{day} 07:30:00"
        sec[f"{day}_daytime_0730"] = {
            "in_service_new": int(c_in_i.loc[c_in_i["slot_start"] == d0730, "vehicles_in_service"].sum()),
            "in_service_old": int(o_in.loc[o_in["slot_start"] == d0730, "vehicles_in_service"].sum()),
            "global_fleet_new": int(c_fl_i.loc[c_fl_i["slot_start"] == d0730, "global_fleet_lower_bound"].drop_duplicates().sum()),
            "global_fleet_old": int(o_fl.loc[o_fl["slot_start"] == d0730, "global_fleet_lower_bound"].drop_duplicates().sum()),
        }
        for hhmm in ("00:00:00", "00:15:00"):
            slot = f"{day} {hhmm}"
            ni = int(c_in_i.loc[c_in_i["slot_start"] == slot, "vehicles_in_service"].sum())
            oi = int(o_in.loc[o_in["slot_start"] == slot, "vehicles_in_service"].sum())
            sec[f"midnight_{slot}"] = {"new": ni, "old": oi, "rebate": ni - oi}
    rep["SECONDARY_chunked_vs_dailyOLD"] = sec

    OUT.write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps(rep, indent=2, default=str))


if __name__ == "__main__":
    main()
