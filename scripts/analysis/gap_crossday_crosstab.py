"""Cross-day x duration 2x2 cross-tab of group1 driver idle-gaps.

Quantifies the *real* benefit boundary of the R5 fix (overnight chain
continuity). Reuses the SAME raw group1 block-gap construction as
``gap_distribution.py`` (read-only input, local R1 carpool merge, cross-day
continuous gaps -- NO pipeline/day-bucketing call), then classifies every
inter-block gap on two axes:

  axis A  crosses_midnight : floor_to_date(gap_start) != floor_to_date(gap_end)
                            (a SHORT gap straddling 00:00 counts as cross-day;
                             a LONG intra-day gap does NOT). gap_start = current
                             block.trip_end, gap_end = next block.trip_start.
  axis B  duration          : <= 360 min ("may count" under new rule)
                              vs > 360 min ("always discarded" by tau_offline_hard)

R5 can only ever rescue gaps that are cross-day AND <= 360 min (intra-day gaps
were never cut by midnight bucketing; cross-day >360 gaps are discarded anyway).

Does not modify any existing source / pipeline file. Writes only NEW artifacts.
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

ORDERS_PATH = "data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz"
IMG_DIR = "outputs/analysis"
REPORT_JSON = os.path.join(IMG_DIR, "gap_crossday_crosstab_report.json")
IMG_PATH = os.path.join(IMG_DIR, "gap_crossday_crosstab.png")

USECOLS = ["driver_id", "departure_time", "finish_time", "service_type"]
TIME_FMT = "%Y-%m-%d %H:%M:%S"
CHUNK = 5_000_000
NS_PER_MIN = 60_000_000_000
NS_PER_DAY = 86_400_000_000_000

TAU_IDLE = 30
TAU_IDLE_BACKED = 90
TAU_OFFLINE_HARD = 360


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- identical load + R1-merge + block construction as gap_distribution.py ---
def load_orders(path: str) -> pd.DataFrame:
    parts = []
    n_raw = 0
    for i, ch in enumerate(pd.read_csv(path, usecols=USECOLS, chunksize=CHUNK)):
        n_raw += len(ch)
        dep = pd.to_datetime(ch["departure_time"], format=TIME_FMT, errors="coerce")
        fin = pd.to_datetime(ch["finish_time"], format=TIME_FMT, errors="coerce")
        valid = dep.notna().to_numpy() & fin.notna().to_numpy()
        sub = pd.DataFrame(
            {
                "driver_id": ch["driver_id"].to_numpy(),
                "start_ns": dep.values.astype("int64"),
                "finish_ns": fin.values.astype("int64"),
                "is_carpool": (ch["service_type"].to_numpy() == "carpool"),
            }
        )[valid]
        sub = sub[sub["finish_ns"] > sub["start_ns"]]
        parts.append(sub)
        log(f"  chunk {i}: kept {len(sub):,} (cum raw {n_raw:,})")
    df = pd.concat(parts, ignore_index=True)
    log(f"Loaded {len(df):,} valid trips.")
    return df


def build_busy_blocks(df: pd.DataFrame) -> pd.DataFrame:
    cp = df[df["is_carpool"]].sort_values(
        ["driver_id", "start_ns", "finish_ns"], kind="mergesort"
    ).reset_index(drop=True)
    if len(cp):
        run_end = cp.groupby("driver_id")["finish_ns"].cummax()
        prev_run = run_end.groupby(cp["driver_id"]).shift()
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
    ex = df[~df["is_carpool"]]
    ex_blocks = pd.DataFrame(
        {"driver_id": ex["driver_id"].to_numpy(), "ts": ex["start_ns"].to_numpy(), "te": ex["finish_ns"].to_numpy()}
    )
    blocks = pd.concat([cp_blocks, ex_blocks], ignore_index=True)
    blocks = blocks.sort_values(["driver_id", "ts", "te"], kind="mergesort").reset_index(drop=True)
    log(f"Built {len(blocks):,} busy blocks.")
    return blocks


def main() -> None:
    os.makedirs(IMG_DIR, exist_ok=True)
    t0 = time.time()
    df = load_orders(ORDERS_PATH)
    blocks = build_busy_blocks(df)

    ts = blocks["ts"].to_numpy()          # int64 ns
    te = blocks["te"].to_numpy()          # int64 ns
    drv = blocks["driver_id"].to_numpy()
    n = len(blocks)

    # group1 = inter-block gaps (next block belongs to same driver). int64-exact.
    has_next = np.zeros(n, dtype=bool)
    has_next[:-1] = drv[:-1] == drv[1:]
    nxt_ts = np.empty(n, dtype=ts.dtype)
    nxt_ts[:-1] = ts[1:]

    gap_start = te[has_next]              # current block.trip_end
    gap_end = nxt_ts[has_next]            # next block.trip_start
    gap_min = (gap_end - gap_start) / NS_PER_MIN
    total = int(gap_start.size)

    # axis A: cross-day = different natural-day floor (exact, integer division).
    crosses = (gap_start // NS_PER_DAY) != (gap_end // NS_PER_DAY)
    # axis B: duration vs tau_offline_hard.
    le360 = gap_min <= TAU_OFFLINE_HARD

    def cell(mask):
        c = int(np.count_nonzero(mask))
        return {"n": c, "pct_of_group1": float(100.0 * c / total)}

    crosstab = {
        "crossday_le360": cell(crosses & le360),
        "crossday_gt360": cell(crosses & ~le360),
        "intraday_le360": cell(~crosses & le360),
        "intraday_gt360": cell(~crosses & ~le360),
    }

    # Interpretation ratios.
    n_cd_le = crosstab["crossday_le360"]["n"]
    n_cd_gt = crosstab["crossday_gt360"]["n"]
    cd_total = n_cd_le + n_cd_gt
    discarded_was_always_discard = float(100.0 * n_cd_gt / cd_total) if cd_total else float("nan")

    # Duration sub-breakdown WITHIN [crossday & <=360].
    cd_le_mask = crosses & le360
    g = gap_min[cd_le_mask]
    seg_0_30 = int(np.count_nonzero(g <= TAU_IDLE))
    seg_30_90 = int(np.count_nonzero((g > TAU_IDLE) & (g <= TAU_IDLE_BACKED)))
    seg_90_360 = int(np.count_nonzero((g > TAU_IDLE_BACKED) & (g <= TAU_OFFLINE_HARD)))
    sub = {
        "seg_0_30":   {"n": seg_0_30,   "pct_of_cell": float(100.0 * seg_0_30 / g.size) if g.size else float("nan"),
                       "pct_of_group1": float(100.0 * seg_0_30 / total)},
        "seg_30_90":  {"n": seg_30_90,  "pct_of_cell": float(100.0 * seg_30_90 / g.size) if g.size else float("nan"),
                       "pct_of_group1": float(100.0 * seg_30_90 / total)},
        "seg_90_360": {"n": seg_90_360, "pct_of_cell": float(100.0 * seg_90_360 / g.size) if g.size else float("nan"),
                       "pct_of_group1": float(100.0 * seg_90_360 / total)},
    }
    rescuable_0_90 = seg_0_30 + seg_30_90
    rescuable_pct_of_group1 = float(100.0 * rescuable_0_90 / total)

    report = {
        "orders_path": ORDERS_PATH,
        "thresholds_min": {"tau_idle": TAU_IDLE, "tau_idle_backed": TAU_IDLE_BACKED, "tau_offline_hard": TAU_OFFLINE_HARD},
        "group1_total_interblock_gaps": total,
        "crosstab": crosstab,
        "crossday_total": {"n": cd_total, "pct_of_group1": float(100.0 * cd_total / total)},
        "pct_of_crossday_that_was_always_discard_gt360": discarded_was_always_discard,
        "crossday_le360_duration_breakdown": sub,
        "R5_rescuable_crossday_0_90min": {"n": rescuable_0_90, "pct_of_group1": rescuable_pct_of_group1},
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(REPORT_JSON, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    log(f"Wrote {REPORT_JSON}")

    # --- optional figure: 2x2 heatmap (% of group1) ---
    mat = np.array([
        [crosstab["crossday_le360"]["pct_of_group1"], crosstab["crossday_gt360"]["pct_of_group1"]],
        [crosstab["intraday_le360"]["pct_of_group1"], crosstab["intraday_gt360"]["pct_of_group1"]],
    ])
    cnt = np.array([
        [crosstab["crossday_le360"]["n"], crosstab["crossday_gt360"]["n"]],
        [crosstab["intraday_le360"]["n"], crosstab["intraday_gt360"]["n"]],
    ])
    fig, ax = plt.subplots(figsize=(7.5, 6))
    im = ax.imshow(mat, cmap="YlOrRd")
    ax.set_xticks([0, 1], ["<=360 min (may count)", ">360 min (always discarded)"])
    ax.set_yticks([0, 1], ["cross-day", "intra-day"])
    for r in range(2):
        for c in range(2):
            ax.text(c, r, f"{mat[r, c]:.2f}%\n(n={cnt[r, c]:,})", ha="center", va="center",
                    fontsize=11, color="black")
    ax.set_title("group1 inter-block gaps: cross-day x duration\n"
                 f"(% of {total:,} group1 gaps; tau_offline_hard=360min)")
    fig.colorbar(im, ax=ax, label="% of group1")
    fig.tight_layout()
    fig.savefig(IMG_PATH, dpi=130)
    plt.close(fig)
    log(f"Wrote {IMG_PATH}")

    print("\n================ CROSS-DAY x DURATION CROSSTAB ================")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
