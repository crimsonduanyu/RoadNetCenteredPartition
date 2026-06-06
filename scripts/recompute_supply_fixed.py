"""
Diagnostic recompute of supply tables with stock-vs-flow metrics.

NOTE: Fix-1 and Fix-2 below are now applied directly in the main supply
pipeline (``src/lib/supply.py``, run via ``src/stages/stage3_supply.py``), so this
script is no longer
the source of truth for those corrections — it remains only for the additional
stock-vs-flow diagnostics. Re-clipping already-clipped intervals is idempotent,
so running it against the corrected main output is safe.

Fix-1 (Critical): Slot expansion is clipped to each interval's natural day.
  - idle_window: natural day = date encoded in chain_id
  - trip_segment: natural day = trip_start.date()
  Eliminates cross-midnight slot bleed that caused 44 % duplicate rows.

Fix-2 (High): fleet_lower_bound_cluster uses origin cluster only for in-service
  drivers (no longer attributes a driver to both origin AND destination).
  Global fleet lower bound is unchanged.

New metrics (stock-vs-flow):
  - idle_driver_minutes: sum of overlap between idle windows and each slot
  - instantaneous_idle: idle_driver_minutes / 15  (time-average idle stock)
  - in_service_driver_minutes: same for in-service trips
  - occupancy: in_service_dm / (in_service_dm + idle_dm)
  - idle_dm_per_order: idle_driver_minutes / demand_orders  (flow-comparable ratio)

Outputs
-------
data/processed/fifth_ring/supply_fixed/
  supply_idle_by_cluster.csv.gz       (slot, cluster) -> available_vehicles, idle_dm
  supply_in_service_od.csv.gz         (slot, O, D) -> vehicles_in_service, in_svc_dm
  supply_fleet_lb.csv.gz              (slot, cluster) -> fleet_lb, global_fleet_lb
outputs/supply_audit_fixed/
  supply_demand_merged.csv.gz         merged table ready for analysis
  (figures and tables written by run_diagnostics section)
"""

from __future__ import annotations
import gc
import os
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ── paths ────────────────────────────────────────────────────────────────────
SUPPLY_IN  = Path("data/processed/fifth_ring/supply")
SUPPLY_OUT = Path("data/processed/fifth_ring/supply_fixed")
DEMAND_PATH = Path("data/processed/fifth_ring/order_pipeline/cluster_od_15min.csv")
AUDIT_DIR  = Path("outputs/supply_audit_fixed")
SUPPLY_OUT.mkdir(parents=True, exist_ok=True)
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

SLOT_MIN = 15
SLOT_NS  = SLOT_MIN * 60 * 1_000_000_000
N_CLUSTERS = 100
PEAK_MORNING = set(range(7, 11))   # 07-10h
PEAK_EVENING = set(range(17, 20))  # 17-19h


# ── core vectorised expansion ─────────────────────────────────────────────────

def expand_day_clipped(
    df: pd.DataFrame,
    start_col: str,
    end_col: str,
    day_ns_col: str,         # int64 ns of natural-day midnight
    extra_cols: list[str],
) -> pd.DataFrame:
    """
    Expand [start, end) intervals to 15-min slot rows, clipped to the
    interval's natural day.  Returns columns:
      slot_start_ns (int64), overlap_minutes (float64), *extra_cols
    """
    s = df[start_col].values.astype(np.int64)
    e = df[end_col].values.astype(np.int64)
    d0 = df[day_ns_col].values.astype(np.int64)
    d1 = d0 + 86_400 * 1_000_000_000  # midnight of next day

    # Clip to natural day
    cs = np.maximum(s, d0)
    ce = np.minimum(e, d1)

    valid = ce > cs
    if not valid.any():
        return pd.DataFrame(columns=["slot_start_ns", "overlap_minutes"] + extra_cols)

    cs, ce = cs[valid], ce[valid]
    extras = {c: df[c].values[valid] for c in extra_cols}

    # First and last slot index
    first_slot = (cs // SLOT_NS) * SLOT_NS
    last_slot  = ((ce - 1) // SLOT_NS) * SLOT_NS
    n_slots    = ((last_slot - first_slot) // SLOT_NS + 1).astype(np.int64)

    # Repeat rows
    rpt = np.repeat(np.arange(len(cs)), n_slots)
    offsets = np.concatenate([np.arange(n) for n in n_slots])

    slot_ns = first_slot[rpt] + offsets * SLOT_NS
    slot_end_ns = slot_ns + SLOT_NS

    ov_start = np.maximum(cs[rpt], slot_ns)
    ov_end   = np.minimum(ce[rpt], slot_end_ns)
    overlap  = (ov_end - ov_start).astype(np.float64) / 6e10   # ns → min

    result = {"slot_start_ns": slot_ns, "overlap_minutes": overlap}
    for c in extra_cols:
        result[c] = extras[c][rpt]
    return pd.DataFrame(result)


# ── Step 1 : idle windows ─────────────────────────────────────────────────────
print("Step 1: processing idle_windows.csv.gz ...")

idle_raw = pd.read_csv(SUPPLY_IN / "idle_windows.csv.gz",
                       usecols=["chain_id", "driver_id", "idle_start", "idle_end", "idle_cluster_id"])
print(f"  loaded {len(idle_raw):,} idle windows")

idle_raw["idle_start"] = pd.to_datetime(idle_raw["idle_start"]).values.astype(np.int64)
idle_raw["idle_end"]   = pd.to_datetime(idle_raw["idle_end"]).values.astype(np.int64)

# Natural day from chain_id: "driverid_YYYY-MM-DD_seq"
date_str = idle_raw["chain_id"].str.rsplit("_", n=2).str[1]
idle_raw["day_ns"] = pd.to_datetime(date_str).values.astype(np.int64)

expanded_idle = expand_day_clipped(
    idle_raw, "idle_start", "idle_end", "day_ns",
    extra_cols=["driver_id", "idle_cluster_id"],
)
del idle_raw; gc.collect()
print(f"  expanded to {len(expanded_idle):,} (slot, idle) rows")

expanded_idle["cluster_id"] = expanded_idle["idle_cluster_id"].astype(str)

# Aggregate to (slot, cluster, driver) → sum overlap (driver may appear >1 window)
idle_by_drv = (
    expanded_idle
    .groupby(["slot_start_ns", "cluster_id", "driver_id"], as_index=False)["overlap_minutes"]
    .sum()
)
del expanded_idle; gc.collect()

# Final: (slot, cluster) → available_vehicles, idle_driver_minutes
idle_agg = (
    idle_by_drv
    .groupby(["slot_start_ns", "cluster_id"], as_index=False)
    .agg(available_vehicles=("driver_id", "nunique"),
         idle_driver_minutes=("overlap_minutes", "sum"))
)
idle_agg["slot_start"] = pd.to_datetime(idle_agg["slot_start_ns"])
idle_agg["instantaneous_idle"] = idle_agg["idle_driver_minutes"] / SLOT_MIN
print(f"  {len(idle_agg):,} (slot, cluster) idle rows after Fix-1")

# Keep driver-level slot rows for fleet LB computation
idle_driver_slots = idle_by_drv[["slot_start_ns", "cluster_id", "driver_id"]].copy()
del idle_by_drv; gc.collect()


# ── Step 2 : trip segments ────────────────────────────────────────────────────
print("Step 2: processing trip_segments.csv.gz ...")

trip_raw = pd.read_csv(SUPPLY_IN / "trip_segments.csv.gz",
                       usecols=["driver_id", "trip_start", "trip_end",
                                "origin_cluster_id", "destination_cluster_id"])
print(f"  loaded {len(trip_raw):,} trip segments")

trip_raw["trip_start"] = pd.to_datetime(trip_raw["trip_start"]).values.astype(np.int64)
trip_raw["trip_end"]   = pd.to_datetime(trip_raw["trip_end"]).values.astype(np.int64)

# Natural day = midnight of trip_start date
day_ns = (trip_raw["trip_start"] // (86_400 * 1_000_000_000)) * (86_400 * 1_000_000_000)
trip_raw["day_ns"] = day_ns.values

expanded_trip = expand_day_clipped(
    trip_raw, "trip_start", "trip_end", "day_ns",
    extra_cols=["driver_id", "origin_cluster_id", "destination_cluster_id"],
)
del trip_raw; gc.collect()
print(f"  expanded to {len(expanded_trip):,} (slot, trip) rows")

expanded_trip["origin_cluster_id"]      = expanded_trip["origin_cluster_id"].astype(str)
expanded_trip["destination_cluster_id"] = expanded_trip["destination_cluster_id"].astype(str)

# (a) vehicles_in_service per OD × slot
in_svc_od = (
    expanded_trip
    .groupby(["slot_start_ns", "origin_cluster_id", "destination_cluster_id", "driver_id"],
             as_index=False)["overlap_minutes"].sum()
    .groupby(["slot_start_ns", "origin_cluster_id", "destination_cluster_id"], as_index=False)
    .agg(vehicles_in_service=("driver_id", "nunique"),
         in_service_driver_minutes=("overlap_minutes", "sum"))
)
in_svc_od["slot_start"] = pd.to_datetime(in_svc_od["slot_start_ns"])

# (b) in-service origin-level for fleet LB (Fix-2: origin only)
trip_origin_slots = (
    expanded_trip[["slot_start_ns", "origin_cluster_id", "driver_id"]]
    .rename(columns={"origin_cluster_id": "cluster_id"})
    .drop_duplicates()
)
del expanded_trip; gc.collect()


# ── Step 3 : fleet lower bound (Fix-2) ───────────────────────────────────────
print("Step 3: computing fleet lower bound (Fix-2: origin-only for in-service) ...")

all_activity = pd.concat(
    [idle_driver_slots,
     trip_origin_slots],
    ignore_index=True,
)
del idle_driver_slots, trip_origin_slots; gc.collect()

fleet_cluster = (
    all_activity
    .drop_duplicates(["slot_start_ns", "cluster_id", "driver_id"])
    .groupby(["slot_start_ns", "cluster_id"], as_index=False)["driver_id"]
    .nunique()
    .rename(columns={"driver_id": "fleet_lower_bound_cluster"})
)
global_fleet = (
    all_activity
    .drop_duplicates(["slot_start_ns", "driver_id"])
    .groupby("slot_start_ns", as_index=False)["driver_id"]
    .nunique()
    .rename(columns={"driver_id": "global_fleet_lower_bound"})
)
fleet_cluster["slot_start"] = pd.to_datetime(fleet_cluster["slot_start_ns"])
global_fleet["slot_start"] = pd.to_datetime(global_fleet["slot_start_ns"])
fleet_lb = fleet_cluster.merge(
    global_fleet[["slot_start_ns", "global_fleet_lower_bound"]],
    on="slot_start_ns", how="left",
)
del all_activity, fleet_cluster, global_fleet; gc.collect()
print(f"  fleet_lb: {len(fleet_lb):,} rows")


# ── Step 4 : merge with in-service duration for occupancy ────────────────────
# in-service driver-minutes per origin cluster × slot (for occupancy)
in_svc_origin = (
    in_svc_od
    .groupby(["slot_start_ns", "origin_cluster_id"], as_index=False)
    .agg(vehicles_in_service_origin=("vehicles_in_service", "sum"),
         in_service_driver_minutes=("in_service_driver_minutes", "sum"))
    .rename(columns={"origin_cluster_id": "cluster_id"})
)

fleet_lb = fleet_lb.merge(
    in_svc_origin[["slot_start_ns", "cluster_id", "in_service_driver_minutes"]],
    on=["slot_start_ns", "cluster_id"], how="left",
)
fleet_lb["in_service_driver_minutes"] = fleet_lb["in_service_driver_minutes"].fillna(0.0)
fleet_lb = fleet_lb.merge(
    idle_agg[["slot_start_ns", "cluster_id", "idle_driver_minutes"]],
    on=["slot_start_ns", "cluster_id"], how="left",
)
fleet_lb["idle_driver_minutes"] = fleet_lb["idle_driver_minutes"].fillna(0.0)
total_dm = fleet_lb["in_service_driver_minutes"] + fleet_lb["idle_driver_minutes"]
fleet_lb["occupancy"] = np.where(
    total_dm > 0,
    fleet_lb["in_service_driver_minutes"] / total_dm,
    np.nan,
)


# ── Step 5 : save supply tables ───────────────────────────────────────────────
print("Step 5: saving fixed supply tables ...")

out_cols_idle = ["slot_start", "cluster_id", "available_vehicles",
                 "idle_driver_minutes", "instantaneous_idle"]
idle_agg[out_cols_idle].to_csv(
    SUPPLY_OUT / "supply_idle_by_cluster.csv.gz", index=False)

out_cols_insvc = ["slot_start", "origin_cluster_id", "destination_cluster_id",
                  "vehicles_in_service", "in_service_driver_minutes"]
in_svc_od["slot_start"] = pd.to_datetime(in_svc_od["slot_start_ns"])
in_svc_od[out_cols_insvc].to_csv(
    SUPPLY_OUT / "supply_in_service_od.csv.gz", index=False)

out_cols_fleet = ["slot_start", "cluster_id", "fleet_lower_bound_cluster",
                  "global_fleet_lower_bound", "in_service_driver_minutes",
                  "idle_driver_minutes", "occupancy"]
fleet_lb[out_cols_fleet].to_csv(
    SUPPLY_OUT / "supply_fleet_lb.csv.gz", index=False)

print("  saved.")


# ── Step 6 : load demand and build analysis table ─────────────────────────────
print("Step 6: loading demand and merging ...")

demand_chunks = []
for chunk in pd.read_csv(DEMAND_PATH, chunksize=500_000):
    chunk["slot_start"] = pd.to_datetime(chunk["slot_start"])
    chunk["origin_cluster_id"] = chunk["origin_cluster_id"].astype(str)
    agg = chunk.groupby(["slot_start", "origin_cluster_id"], as_index=False)["total_count"].sum()
    demand_chunks.append(agg)
demand = (
    pd.concat(demand_chunks)
    .groupby(["slot_start", "origin_cluster_id"], as_index=False)["total_count"]
    .sum()
    .rename(columns={"origin_cluster_id": "cluster_id", "total_count": "demand_orders"})
)
del demand_chunks; gc.collect()

# Build the core analysis table from idle (Fix-1 corrected)
ana = idle_agg[["slot_start", "cluster_id", "available_vehicles",
                "idle_driver_minutes", "instantaneous_idle"]].copy()
ana = ana.merge(demand, on=["slot_start", "cluster_id"], how="outer")
ana["available_vehicles"]    = ana["available_vehicles"].fillna(0).astype(int)
ana["idle_driver_minutes"]   = ana["idle_driver_minutes"].fillna(0.0)
ana["instantaneous_idle"]    = ana["instantaneous_idle"].fillna(0.0)
ana["demand_orders"]         = ana["demand_orders"].fillna(0).astype(int)

# Merge in-service origin level
ana = ana.merge(
    in_svc_origin.assign(slot_start=pd.to_datetime(
        in_svc_origin["slot_start_ns"]))[["slot_start","cluster_id","vehicles_in_service_origin"]],
    on=["slot_start","cluster_id"], how="left",
)
ana["vehicles_in_service_origin"] = ana["vehicles_in_service_origin"].fillna(0).astype(int)

# Merge fleet LB
ana = ana.merge(
    fleet_lb[["slot_start","cluster_id","fleet_lower_bound_cluster",
              "global_fleet_lower_bound","occupancy"]],
    on=["slot_start","cluster_id"], how="left",
)
ana["fleet_lower_bound_cluster"] = ana["fleet_lower_bound_cluster"].fillna(0).astype(int)

# Time features
ana["hour"] = ana["slot_start"].dt.hour
ana["time_period"] = np.select(
    [ana["hour"].isin(PEAK_MORNING), ana["hour"].isin(PEAK_EVENING)],
    ["morning_peak", "evening_peak"], default="off_peak",
)

# ── Core ratio metrics ────────────────────────────────────────────────────────
# Count-based: idle vehicles / demand
ana["idle_per_demand_count"] = ana["available_vehicles"] / ana["demand_orders"].clip(lower=1)
# Stock-based: idle driver-minutes / demand (= instantaneous_idle / demand_rate)
ana["idle_dm_per_order"]     = ana["idle_driver_minutes"] / ana["demand_orders"].clip(lower=1)
# Instantaneous idle / instantaneous demand rate (× slot_min for dimensionless ratio)
# idle_rate = instantaneous_idle [drivers]; demand_rate = demand_orders / 15 [orders/min]
# ratio = idle_rate / demand_rate = (idle_dm/15) / (demand/15) = idle_dm / demand
# so idle_dm_per_order IS the dimensionless ratio (if service time = 1 slot, value of 1 means break-even)
# We'll report it as is and note units

# Window-inflation factor
ana["window_inflation"] = np.where(
    ana["instantaneous_idle"] > 0,
    ana["available_vehicles"] / ana["instantaneous_idle"],
    np.nan,
)

ana.to_csv(AUDIT_DIR / "supply_demand_merged.csv.gz", index=False)
print(f"  analysis table: {len(ana):,} rows  ({ana['slot_start'].nunique():,} slots, "
      f"{ana['cluster_id'].nunique()} clusters)")


# ── Step 7 : DIAGNOSTICS ─────────────────────────────────────────────────────
print("Step 7: running diagnostics ...")

# Only rows where SOMETHING is active (idle OR demand > 0)
active = ana[(ana["available_vehicles"] > 0) | (ana["demand_orders"] > 0)].copy()
print(f"  active pairs: {len(active):,}")

# ─── DIAG 1: Superabundant slots with idle (count-based) ──────────────────
print("\n=== DIAG 1: idle > N × demand (count-based) ===")
N = len(active)
for t in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
    n = (active["available_vehicles"] > active["demand_orders"] * t).sum()
    print(f"  idle_count > {t:.1f}× demand: {n:,} / {N:,} = {n/N:.1%}")

# Demand=0 but idle>0 (pure supply, no observable demand)
n_d0 = ((active["demand_orders"] == 0) & (active["available_vehicles"] > 0)).sum()
n_i0 = ((active["demand_orders"] > 0) & (active["available_vehicles"] == 0)).sum()
print(f"  demand=0 but idle>0: {n_d0:,} ({n_d0/N:.1%})")
print(f"  idle=0 but demand>0: {n_i0:,} ({n_i0/N:.1%})")

# ─── DIAG 1b: Stock-based (idle_dm_per_order) ─────────────────────────────
print("\n=== DIAG 1b: idle_dm_per_order distribution ===")
# idle_dm_per_order = idle_driver_minutes / demand_orders
# Units: [driver·min / order]  (≈ idle minutes available per order)
# Break-even (1 driver idle for 1 min per order) = 1.  Typical trip ≈ 10 min → break-even ≈ 10.
active_d = active[active["demand_orders"] > 0].copy()
print(f"  slots with demand>0: {len(active_d):,}")
print(active_d["idle_dm_per_order"].describe(percentiles=[.1,.25,.5,.75,.9,.95,.99]))
for t in [1.0, 5.0, 10.0, 20.0, 50.0]:
    n = (active_d["idle_dm_per_order"] > t).sum()
    print(f"  idle_dm_per_order > {t:.0f}: {n:,}/{len(active_d):,} = {n/len(active_d):.1%}")

# ─── DIAG 2: omega distribution by time period ────────────────────────────
print("\n=== DIAG 2: idle_count_per_demand by time period ===")
for period in ["morning_peak", "evening_peak", "off_peak", "ALL"]:
    sub = active_d if period == "ALL" else active_d[active_d["time_period"] == period]
    print(f"\n{period} (n={len(sub):,}):")
    print(sub["idle_per_demand_count"].describe(percentiles=[.1,.25,.5,.75,.9,.95]).to_string())
print("\n=== idle_dm_per_order by time period ===")
for period in ["morning_peak", "evening_peak", "off_peak", "ALL"]:
    sub = active_d if period == "ALL" else active_d[active_d["time_period"] == period]
    print(f"\n{period} (n={len(sub):,}):")
    print(sub["idle_dm_per_order"].describe(percentiles=[.1,.25,.5,.75,.9,.95]).to_string())

# ─── DIAG 3: Window inflation factor ─────────────────────────────────────
print("\n=== DIAG 3: Window inflation factor (available_vehicles / instantaneous_idle) ===")
wi = active[active["instantaneous_idle"] > 0]["window_inflation"]
print(wi.describe(percentiles=[.1,.25,.5,.75,.9,.95]))

# ─── DIAG 4: Occupancy / utilization ─────────────────────────────────────
print("\n=== DIAG 4: Occupancy (in_service_dm / total_dm) ===")
occ = active.dropna(subset=["occupancy"])["occupancy"]
print(f"  slots with valid occupancy: {len(occ):,}")
print(occ.describe(percentiles=[.1,.25,.5,.75,.9,.95]))

# ─── DIAG 5: UNDERSUPPLY DETECTION (the key question for Φ identifiability) ─
print("\n=== DIAG 5: Undersupply detection (idle / demand by period and cluster) ===")

# Count-based breakpoints
bps = [0.25, 0.5, 1.0, 1.5, 2.0, 3.0]
print("\nCount ratio (available_vehicles / demand_orders) — by time period:")
header = "period          " + "  ".join(f"<{b:.2f}" for b in bps)
print(header)
for period in ["morning_peak", "evening_peak", "off_peak", "ALL"]:
    sub = active_d if period == "ALL" else active_d[active_d["time_period"] == period]
    row = f"{period:<16}"
    for b in bps:
        pct = (sub["idle_per_demand_count"] < b).mean() * 100
        row += f"  {pct:5.1f}%"
    print(row)

print("\nStock ratio (idle_dm_per_order in driver·min/order) — by time period:")
# Service time proxy: assume ~10 min average trip → break-even idle_dm_per_order ≈ 10
bps_dm = [1.0, 5.0, 10.0, 15.0, 30.0]
header2 = "period          " + "  ".join(f"<{b:.0f}" for b in bps_dm)
print(header2)
for period in ["morning_peak", "evening_peak", "off_peak", "ALL"]:
    sub = active_d if period == "ALL" else active_d[active_d["time_period"] == period]
    row = f"{period:<16}"
    for b in bps_dm:
        pct = (sub["idle_dm_per_order"] < b).mean() * 100
        row += f"  {pct:5.1f}%"
    print(row)

# Per-cluster: fraction of slots where idle/demand < 2 (near breakpoint)
cluster_stats = active_d.groupby("cluster_id").agg(
    n_active_slots=("slot_start", "count"),
    median_idle_per_d=("idle_per_demand_count", "median"),
    median_idle_dm_per_order=("idle_dm_per_order", "median"),
    pct_undersupply_count=("idle_per_demand_count", lambda x: (x < 1).mean() * 100),
    pct_near_breakpoint=("idle_per_demand_count", lambda x: (x < 2).mean() * 100),
    pct_undersupply_stock=("idle_dm_per_order", lambda x: (x < 10).mean() * 100),
    mean_occupancy=("occupancy", "mean"),
).sort_values("median_idle_per_d")
print("\nPer-cluster undersupply summary (sorted by median idle/demand):")
print(cluster_stats.round(2).to_string())
cluster_stats.to_csv(AUDIT_DIR / "cluster_undersupply.csv")


# ── Step 8 : Figures ──────────────────────────────────────────────────────────
print("\nStep 8: generating figures ...")

# ─── Fig 1: idle/demand count ratio distribution by period ────────────────
fig, axes = plt.subplots(1, 3, figsize=(15, 4))
periods_info = [
    ("morning_peak", "Morning Peak 07-10h", "#e74c3c"),
    ("evening_peak", "Evening Peak 17-19h", "#e67e22"),
    ("off_peak",     "Off-Peak",            "#3498db"),
]
for ax, (period, label, color) in zip(axes, periods_info):
    sub = active_d[active_d["time_period"] == period]["idle_per_demand_count"].clip(upper=15)
    ax.hist(sub, bins=60, color=color, alpha=0.8, edgecolor="white", linewidth=0.3)
    med = sub.median()
    ax.axvline(1.0, color="black", lw=1.5, ls="--", label="idle=demand")
    ax.axvline(2.0, color="darkred", lw=1, ls=":", label="idle=2×demand")
    ax.axvline(med, color="green", lw=1.5, ls="-", label=f"median={med:.2f}")
    ax.set_title(label, fontsize=10)
    ax.set_xlabel("idle_vehicles / demand_orders (clipped @15)", fontsize=8)
    ax.set_ylabel("Slot count", fontsize=8)
    ax.legend(fontsize=6.5)
    n_under = (sub < 1.0).mean()
    ax.text(0.97, 0.95, f"n={len(sub):,}\n<1.0: {n_under:.1%}",
            transform=ax.transAxes, ha="right", va="top", fontsize=7.5,
            bbox=dict(boxstyle="round", facecolor="white", alpha=0.7))
plt.suptitle("Idle / Demand ratio (Fix-1 corrected, count-based)", y=1.01, fontsize=11)
plt.tight_layout()
plt.savefig(AUDIT_DIR / "fig1_idle_demand_count.png", dpi=150, bbox_inches="tight")
plt.close()

# ─── Fig 2: stock-based ratio (idle_dm_per_order) distribution ───────────
fig, axes = plt.subplots(1, 3, figsize=(15, 4))
for ax, (period, label, color) in zip(axes, periods_info):
    sub = active_d[active_d["time_period"] == period]["idle_dm_per_order"].clip(upper=100)
    ax.hist(sub, bins=60, color=color, alpha=0.8, edgecolor="white", linewidth=0.3)
    med = sub.median()
    ax.axvline(10.0, color="black", lw=1.5, ls="--", label="10 min/order (break-even est.)")
    ax.axvline(med, color="green", lw=1.5, ls="-", label=f"median={med:.1f}")
    ax.set_title(label, fontsize=10)
    ax.set_xlabel("idle driver-min per order (clipped @100)", fontsize=8)
    ax.set_ylabel("Slot count", fontsize=8)
    ax.legend(fontsize=6.5)
    n_low = (sub < 10.0).mean()
    ax.text(0.97, 0.95, f"n={len(sub):,}\n<10: {n_low:.1%}",
            transform=ax.transAxes, ha="right", va="top", fontsize=7.5,
            bbox=dict(boxstyle="round", facecolor="white", alpha=0.7))
plt.suptitle("Stock-corrected ratio: idle driver-min / demand_orders (Fix-1 corrected)", y=1.01, fontsize=11)
plt.tight_layout()
plt.savefig(AUDIT_DIR / "fig2_idle_dm_per_order.png", dpi=150, bbox_inches="tight")
plt.close()

# ─── Fig 3: count vs stock scatter (window-inflation visible) ────────────
sample = active_d.sample(min(80000, len(active_d)), random_state=42)
fig, ax = plt.subplots(figsize=(7, 6))
sc = ax.scatter(
    sample["available_vehicles"].clip(upper=200),
    sample["instantaneous_idle"].clip(upper=200),
    c=sample["demand_orders"].clip(upper=100),
    alpha=0.12, s=3, cmap="viridis", rasterized=True,
)
lim = 200
ax.plot([0, lim], [0, lim], "r--", lw=1.5, label="count = stock (no window inflation)")
plt.colorbar(sc, ax=ax, label="demand_orders (clipped @100)")
ax.set_xlabel("available_vehicles (count-based, distinct idle drivers)", fontsize=9)
ax.set_ylabel("instantaneous_idle = idle_dm / 15 (stock-based)", fontsize=9)
ax.set_title("Window inflation: count vs stock\n(Points below diagonal = count inflated by short idle windows)", fontsize=9)
ax.legend(fontsize=8)
plt.tight_layout()
plt.savefig(AUDIT_DIR / "fig3_count_vs_stock.png", dpi=150, bbox_inches="tight")
plt.close()

# ─── Fig 4: Undersupply heatmap (cluster × hour) — CORE for Φ ID ─────────
# metric: fraction of slots with idle_per_demand < 1 (undersupplied by count)
hmap = active_d.groupby(["cluster_id", "hour"])["idle_per_demand_count"].apply(
    lambda x: (x < 1).mean()
).unstack(fill_value=0)
row_order = hmap.mean(axis=1).sort_values(ascending=False).index
hmap = hmap.loc[row_order]
fig, ax = plt.subplots(figsize=(18, 12))
im = ax.imshow(hmap.values, aspect="auto", cmap="RdYlGn_r",
               vmin=0, vmax=max(0.01, hmap.values.max()), interpolation="nearest")
ax.set_xticks(range(24))
ax.set_xticklabels([f"{h:02d}h" for h in range(24)], rotation=45, ha="right", fontsize=7)
ax.set_yticks(range(len(row_order)))
ax.set_yticklabels([f"C{c}" for c in row_order], fontsize=6)
ax.set_title("Fraction of slots with idle < demand (undersupply)\nRed = high undersupply rate — KEY for Phi identifiability",
             fontsize=10)
plt.colorbar(im, ax=ax, label="Fraction undersupplied (idle < demand)")
plt.tight_layout()
plt.savefig(AUDIT_DIR / "fig4_undersupply_heatmap.png", dpi=150, bbox_inches="tight")
plt.close()

# ─── Fig 5: occupancy by hour ─────────────────────────────────────────────
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
for period, color, lbl in [("morning_peak", "red", "Morning Peak"),
                             ("evening_peak", "orange", "Evening Peak"),
                             ("off_peak", "steelblue", "Off-Peak")]:
    sub_h = active[active["time_period"] == period].groupby("hour")["occupancy"].mean()
    ax1.plot(sub_h.index, sub_h.values, color=color, lw=2, label=lbl)
ax1.set_ylabel("Mean occupancy = in_service_dm / total_dm", fontsize=9)
ax1.set_title("Driver occupancy (utilization) by hour", fontsize=10)
ax1.legend(fontsize=8); ax1.grid(alpha=0.3)

for period, color, lbl in [("morning_peak", "red", "Morning Peak"),
                             ("evening_peak", "orange", "Evening Peak"),
                             ("off_peak", "steelblue", "Off-Peak")]:
    sub_h = active_d[active_d["time_period"] == period].groupby("hour")["idle_per_demand_count"].median()
    ax2.plot(sub_h.index, sub_h.values, color=color, lw=2, label=lbl)
ax2.axhline(1.0, color="black", lw=1, ls=":")
ax2.set_xlabel("Hour of day"); ax2.set_ylabel("Median idle / demand (count)", fontsize=9)
ax2.set_title("Idle / demand ratio by hour", fontsize=10)
ax2.legend(fontsize=8); ax2.grid(alpha=0.3)
ax2.set_xticks(range(0, 24, 2))
plt.tight_layout()
plt.savefig(AUDIT_DIR / "fig5_occupancy_and_idle_by_hour.png", dpi=150, bbox_inches="tight")
plt.close()

# ─── Fig 6: undersupply CDF comparison by region type ────────────────────
# Compare top-10 and bottom-10 clusters by median idle/demand
top10 = cluster_stats.tail(10).index.tolist()   # highest idle/demand = most oversupplied
bot10 = cluster_stats.head(10).index.tolist()   # lowest = closest to balanced

fig, ax = plt.subplots(figsize=(9, 5))
for cids, label, color in [(top10, "Top-10 (most oversupplied)", "steelblue"),
                             (bot10, "Bottom-10 (least oversupplied)", "firebrick")]:
    vals = active_d[active_d["cluster_id"].isin([str(c) for c in cids])]["idle_per_demand_count"].clip(upper=20)
    vals_sorted = np.sort(vals)
    ax.plot(vals_sorted, np.linspace(0, 1, len(vals_sorted)), lw=2, color=color, label=label)
ax.axvline(1.0, color="black", lw=1.5, ls="--", label="idle=demand")
ax.axvline(2.0, color="gray", lw=1, ls=":", label="idle=2×demand")
ax.set_xlabel("idle_vehicles / demand_orders (clipped @20)")
ax.set_ylabel("CDF")
ax.set_title("Undersupply CDF: most vs least oversupplied clusters\n(X < 1 = genuinely undersupplied; useful region for Phi identification)")
ax.legend(fontsize=8); ax.grid(alpha=0.3)
plt.tight_layout()
plt.savefig(AUDIT_DIR / "fig6_undersupply_cdf.png", dpi=150, bbox_inches="tight")
plt.close()

print("\nAll figures saved.")

# ── Step 9: Save summary tables ───────────────────────────────────────────────
print("Step 9: saving summary tables ...")

period_stats = active_d.groupby("time_period").agg(
    n=("slot_start", "count"),
    median_idle_count_ratio=("idle_per_demand_count", "median"),
    p10_idle_count=("idle_per_demand_count", lambda x: x.quantile(0.10)),
    p90_idle_count=("idle_per_demand_count", lambda x: x.quantile(0.90)),
    pct_count_lt_1=("idle_per_demand_count", lambda x: (x < 1).mean() * 100),
    pct_count_lt_2=("idle_per_demand_count", lambda x: (x < 2).mean() * 100),
    median_idle_dm_per_order=("idle_dm_per_order", "median"),
    pct_dm_lt_10=("idle_dm_per_order", lambda x: (x < 10).mean() * 100),
).round(3)
period_stats.to_csv(AUDIT_DIR / "period_summary.csv")
print(period_stats.to_string())

print("\nDone. All outputs in", AUDIT_DIR, "and", SUPPLY_OUT)
