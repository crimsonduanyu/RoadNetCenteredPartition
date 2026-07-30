"""One-off, standalone analysis of the raw driver idle-gap distribution.

GOAL
----
Read out candidate values for three idle-gap thresholds (tau_idle,
tau_idle_backed, tau_offline_hard) directly from the *raw* gap distribution.

IMPORTANT (why this script does NOT reuse the pipeline)
------------------------------------------------------
The production chain (`roadnet_partition.downstream.supply.reconstruct_driver_chains`) buckets
orders by departure day and forces a chain break at every midnight, which
*erases* cross-day gaps and pollutes the right tail of the gap distribution.
This script therefore re-derives gaps from scratch:

  1. Read the SAME supply input (orders_region_assigned.csv.gz), read-only.
  2. Replicate ONLY the R1 carpool interval-merge semantics
     (cummax(finish) union of overlapping carpool orders) with a local,
     self-contained implementation -- no import of any pipeline function that
     could trigger day-bucketing.
  3. Per driver, sort busy blocks by trip_start and compute
     gap = next_block.trip_start - cur_block.trip_end, CONTINUOUSLY across
     midnight (no day break).

has_next definition (per the task brief)
----------------------------------------
  group1 (has_next=True) : every inter-block gap (a later block exists -> the
                           driver is confirmed to keep operating after the gap).
  group2 (has_next=False): the dangling tail of each driver -- last block's
                           finish to the global window end. This is only a
                           crude proxy (we cannot observe the true end state in
                           a finite window); group1 is the primary analysis.

This script writes only diagnostic artifacts under artifacts/archive/ and does not
modify any existing source or pipeline file.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# --------------------------------------------------------------------------
# Paths (relative to repo root; run from repo root)
# --------------------------------------------------------------------------
ORDERS_PATH = "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
IMG_DIR = "artifacts/archive/supply-gap-diagnostics-v1"
REPORT_JSON = os.path.join(IMG_DIR, "gap_distribution_report.json")
IMG_LINEAR = os.path.join(IMG_DIR, "gap_dist_by_hasnext.png")
IMG_LOG = os.path.join(IMG_DIR, "gap_dist_by_hasnext_log.png")

USECOLS = ["driver_id", "departure_time", "finish_time", "service_type"]
TIME_FMT = "%Y-%m-%d %H:%M:%S"
CHUNK = 5_000_000
NS_PER_MIN = 60_000_000_000.0

# Candidate threshold reference lines (minutes).
REF_LINES = [30, 90, 360]
AXIS_MAX_MIN = 1440  # plot range; gaps > this are reported but not drawn.


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# 1. Load only the columns we need, parse times to int64 ns, keep valid trips.
# --------------------------------------------------------------------------
def load_orders(path: str) -> pd.DataFrame:
    parts = []
    n_raw = 0
    reader = pd.read_csv(path, usecols=USECOLS, chunksize=CHUNK)
    for i, ch in enumerate(reader):
        n_raw += len(ch)
        dep = pd.to_datetime(ch["departure_time"], format=TIME_FMT, errors="coerce")
        fin = pd.to_datetime(ch["finish_time"], format=TIME_FMT, errors="coerce")
        valid = dep.notna().to_numpy() & fin.notna().to_numpy()
        start_ns = dep.values.astype("int64")
        finish_ns = fin.values.astype("int64")
        sub = pd.DataFrame(
            {
                "driver_id": ch["driver_id"].to_numpy(),
                "start_ns": start_ns,
                "finish_ns": finish_ns,
                "is_carpool": (ch["service_type"].to_numpy() == "carpool"),
            }
        )
        sub = sub[valid]
        # R0 validity: positive trip interval (same filter as filter_valid_orders).
        sub = sub[sub["finish_ns"] > sub["start_ns"]]
        parts.append(sub)
        log(f"  chunk {i}: read {len(ch):,} rows, kept {len(sub):,} (cum raw {n_raw:,})")
    df = pd.concat(parts, ignore_index=True)
    del parts
    log(f"Loaded {len(df):,} valid trips out of {n_raw:,} raw rows.")
    df.attrs["n_raw_rows"] = n_raw
    return df


# --------------------------------------------------------------------------
# 2. R1 carpool interval-merge (local re-implementation, no pipeline import).
#    Overlapping carpool orders of the same driver are unioned via cummax(finish).
#    Exclusive orders each remain their own busy block.
# --------------------------------------------------------------------------
def build_busy_blocks(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    cp = df[df["is_carpool"]].sort_values(
        ["driver_id", "start_ns", "finish_ns"], kind="mergesort"
    ).reset_index(drop=True)
    n_cp_orders = len(cp)

    if n_cp_orders:
        run_end = cp.groupby("driver_id")["finish_ns"].cummax()
        prev_run = run_end.groupby(cp["driver_id"]).shift()
        # New group when no predecessor OR no overlap with running union (gap>=0).
        new_grp = prev_run.isna() | (cp["start_ns"] >= prev_run)
        grp = new_grp.groupby(cp["driver_id"]).cumsum().astype("int64")
        cp_blocks = (
            cp.assign(_g=grp)
            .groupby(["driver_id", "_g"], sort=False)
            .agg(ts=("start_ns", "min"), te=("finish_ns", "max"))
            .reset_index()[["driver_id", "ts", "te"]]
        )
    else:
        cp_blocks = pd.DataFrame(columns=["driver_id", "ts", "te"])
    n_cp_groups = len(cp_blocks)

    ex = df[~df["is_carpool"]]
    ex_blocks = pd.DataFrame(
        {
            "driver_id": ex["driver_id"].to_numpy(),
            "ts": ex["start_ns"].to_numpy(),
            "te": ex["finish_ns"].to_numpy(),
        }
    )

    blocks = pd.concat([cp_blocks, ex_blocks], ignore_index=True)
    blocks = blocks.sort_values(["driver_id", "ts", "te"], kind="mergesort").reset_index(drop=True)

    absorbed = n_cp_orders - n_cp_groups
    carpool_stats = {
        "n_carpool_orders": int(n_cp_orders),
        "n_exclusive_orders": int(len(ex)),
        "n_carpool_groups_after_merge": int(n_cp_groups),
        "carpool_orders_absorbed_by_merge": int(absorbed),
        "pct_carpool_orders_absorbed": float(100.0 * absorbed / n_cp_orders) if n_cp_orders else float("nan"),
        "pct_all_orders_absorbed": float(100.0 * absorbed / len(df)) if len(df) else float("nan"),
        "n_busy_blocks": int(len(blocks)),
    }
    log(f"Built {len(blocks):,} busy blocks. Carpool absorbed: {absorbed:,} "
        f"({carpool_stats['pct_carpool_orders_absorbed']:.2f}% of carpool orders).")
    return blocks, carpool_stats


# --------------------------------------------------------------------------
# 3. Cross-day gaps + has_next grouping.
# --------------------------------------------------------------------------
def compute_gaps(blocks: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, dict]:
    next_ts = blocks.groupby("driver_id")["ts"].shift(-1)
    has_next = next_ts.notna().to_numpy()

    # group1: inter-block gaps (minutes). next_ts is float (NaN at tail) -> ok.
    gap_min = (next_ts.to_numpy() - blocks["te"].to_numpy()) / NS_PER_MIN
    g1 = gap_min[has_next]

    # group2 proxy: last block of each driver -> global window end.
    window_end = int(blocks["te"].max())
    tail_min = (window_end - blocks["te"].to_numpy()[~has_next]) / NS_PER_MIN
    g2 = tail_min

    n_drivers = int(blocks["driver_id"].nunique())
    meta = {
        "n_drivers": n_drivers,
        "n_inter_block_gaps_group1": int(g1.size),
        "n_tail_windows_group2": int(g2.size),
        "window_end": pd.to_datetime(window_end).strftime(TIME_FMT),
        "group1_nonpositive_gaps": int(np.sum(g1 <= 0)),
        "group1_negative_gaps": int(np.sum(g1 < 0)),
        "group1_zero_gaps": int(np.sum(g1 == 0)),
    }
    log(f"group1 gaps={g1.size:,}, group2 tails={g2.size:,}, "
        f"group1 non-positive={meta['group1_nonpositive_gaps']:,}.")
    return g1, g2, meta


# --------------------------------------------------------------------------
# 4. Descriptive stats / quantiles (computed on POSITIVE gaps only -> idle gaps).
# --------------------------------------------------------------------------
def describe(name: str, gaps_min: np.ndarray) -> dict:
    pos = gaps_min[gaps_min > 0]
    if pos.size == 0:
        return {"group": name, "n_positive": 0}
    q = np.percentile(pos, [50, 75, 90, 95, 99])
    return {
        "group": name,
        "n_total": int(gaps_min.size),
        "n_positive": int(pos.size),
        "mean_min": float(pos.mean()),
        "std_min": float(pos.std()),
        "min_min": float(pos.min()),
        "max_min": float(pos.max()),
        "p50": float(q[0]),
        "p75": float(q[1]),
        "p90": float(q[2]),
        "p95": float(q[3]),
        "p99": float(q[4]),
        "pct_lt_30": float(100.0 * np.mean(pos < 30)),
        "pct_lt_90": float(100.0 * np.mean(pos < 90)),
        "pct_30_to_360": float(100.0 * np.mean((pos >= 30) & (pos <= 360))),
        "pct_gt_360": float(100.0 * np.mean(pos > 360)),
        "pct_gt_1440": float(100.0 * np.mean(pos > 1440)),
    }


# --------------------------------------------------------------------------
# 5. Plots.
# --------------------------------------------------------------------------
def make_plots(g1: np.ndarray, g2: np.ndarray) -> None:
    bins = np.arange(0, AXIS_MAX_MIN + 5, 5)  # 5-minute bins
    g1p = g1[(g1 > 0) & (g1 <= AXIS_MAX_MIN)]
    g2p = g2[(g2 > 0) & (g2 <= AXIS_MAX_MIN)]

    for logy, path, title_suffix in [
        (False, IMG_LINEAR, "linear y"),
        (True, IMG_LOG, "log y"),
    ]:
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.hist(g1p, bins=bins, alpha=0.5, color="#1f77b4",
                label=f"group1 has_next=True (inter-block gaps, n={g1p.size:,})")
        ax.hist(g2p, bins=bins, alpha=0.5, color="#d62728",
                label=f"group2 has_next=False (tail proxy, n={g2p.size:,})")
        for x in REF_LINES:
            ax.axvline(x, color="k", linestyle="--", linewidth=1, alpha=0.7)
            ax.text(x, ax.get_ylim()[1] * 0.92, f"{x}m", rotation=90,
                    va="top", ha="right", fontsize=8)
        if logy:
            ax.set_yscale("log")
        ax.set_xlim(0, AXIS_MAX_MIN)
        ax.set_xlabel("gap (minutes)  [0-1440 shown; longer gaps reported in JSON]")
        ax.set_ylabel("count (5-min bins)")
        ax.set_title(f"Raw driver idle-gap distribution by has_next ({title_suffix})\n"
                     f"cross-day continuous; carpool overlaps merged (R1)")
        ax.legend(loc="upper right", fontsize=9)
        fig.tight_layout()
        fig.savefig(path, dpi=130)
        plt.close(fig)
        log(f"Wrote {path}")


def tail_stats(name: str, gaps_min: np.ndarray) -> dict:
    pos = gaps_min[gaps_min > 0]
    n = pos.size if pos.size else 1
    return {
        "group": name,
        "n_positive": int(pos.size),
        "n_gt_360": int(np.sum(pos > 360)),
        "pct_gt_360": float(100.0 * np.sum(pos > 360) / n),
        "n_gt_1440": int(np.sum(pos > 1440)),
        "pct_gt_1440": float(100.0 * np.sum(pos > 1440) / n),
        "max_min": float(pos.max()) if pos.size else float("nan"),
        "max_hours": float(pos.max() / 60.0) if pos.size else float("nan"),
        "max_days": float(pos.max() / 1440.0) if pos.size else float("nan"),
    }


def cumulative_table(name: str, gaps_min: np.ndarray) -> dict:
    """Cumulative % of POSITIVE gaps below a set of fine cutpoints, to locate
    the regime change (tau_idle elbow, tau_idle_backed shoulder, plateau onset)."""
    pos = gaps_min[gaps_min > 0]
    cuts = [15, 30, 45, 60, 90, 120, 180, 240, 300, 360, 480, 720, 1440]
    n = pos.size if pos.size else 1
    cum = {f"pct_lt_{c}min": float(100.0 * np.sum(pos < c) / n) for c in cuts}
    # Plateau flatness check: per-5min-bin counts in [360,1440] -- a flat (uniform)
    # tail => low coeff. of variation, evidencing an "offline/next-day" regime.
    bins = np.arange(360, 1440 + 5, 5)
    hist, _ = np.histogram(pos[(pos >= 360) & (pos <= 1440)], bins=bins)
    plateau = {
        "plateau_360_1440_bin_mean": float(hist.mean()),
        "plateau_360_1440_bin_std": float(hist.std()),
        "plateau_360_1440_cv": float(hist.std() / hist.mean()) if hist.mean() else float("nan"),
    }
    return {"group": name, "cumulative_pct": cum, **plateau}


def main() -> None:
    os.makedirs(IMG_DIR, exist_ok=True)
    t0 = time.time()
    log(f"Reading {ORDERS_PATH} ...")
    df = load_orders(ORDERS_PATH)
    n_raw = df.attrs.get("n_raw_rows", len(df))

    blocks, carpool_stats = build_busy_blocks(df)
    g1, g2, meta = compute_gaps(blocks)

    desc = {"group1_has_next_true": describe("group1", g1),
            "group2_has_next_false": describe("group2", g2)}
    tails = {"group1": tail_stats("group1", g1), "group2": tail_stats("group2", g2)}
    cumulative = {"group1": cumulative_table("group1", g1),
                  "group2": cumulative_table("group2", g2)}

    make_plots(g1, g2)

    report = {
        "orders_path": ORDERS_PATH,
        "n_raw_rows": int(n_raw),
        "n_valid_trips": int(len(df)),
        "carpool_merge": carpool_stats,
        "gap_meta": meta,
        "quantiles": desc,
        "cumulative": cumulative,
        "long_tail": tails,
        "reference_thresholds_min": REF_LINES,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(REPORT_JSON, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    log(f"Wrote {REPORT_JSON}")

    # Console summary.
    print("\n================ GAP DISTRIBUTION SUMMARY ================")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
