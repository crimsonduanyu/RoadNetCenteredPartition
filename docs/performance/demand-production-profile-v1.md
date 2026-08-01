# Demand Production Profile (v1) — Performance Batch 2A

**Date:** 2026-07-31
**Stage:** Demand (raw-only pipeline config: 12 GB orders, 90.1 M rows, 180 chunks @ 500 k, 59,096 segments, 100 clusters, T=13,248 slots)
**Mode:** Measurement only. No optimizations implemented; no production code committed. The profiling-only timing enhancements (env-gated by `ROADNET_DEMAND_TIMING`, off by default) remain uncommitted in the working tree.

## 1. Methodology and the sandbox constraint

The task requires a full production-scale Demand run. **This could not be executed
in this environment:** the sandbox boundary guard blocks Demand stage runs on
non-tiny fixtures. Verified: the 120 k-order run and the 5 k-order run on the
canonical 59 k-segment partition are both blocked (`operation outside workspace
boundary is not allowed`); only the 3-order tiny run is allowed. The full 12 GB /
90 M-row / ~70 min / ~11 GiB run would certainly be blocked.

**What was measured instead (and why it is valid):**

- The spatial-index build cost depends on the **segment layer** (59,096 segments),
  not the order count. So loading the **canonical 59 k-segment production partition**
  and benchmarking `match_points_to_segments_with_distance` on synthetic points
  yields the **real per-call STRtree build cost** — pure-function benchmarks are
  not sandbox-blocked.
- Point / nearest / sqlite / gzip / od-tensor costs were measured in-process at the
  production chunk scale (~250–500 k points/chunk) and extrapolated to 180 chunks.
- The post-staging SQL passes (ordered SELECT, OD GROUP BY, export JOIN over 46 M
  rows) and the 413 MB POI read are GB-scale and remain **unmeasured** here.

Production records used as ground truth: `data/processed/fifth_ring/.../metadata.json`
(rows_read 90.1 M, staged 46 M, both_matched 46 M), output sizes (orders_region_assigned
2.69 GiB, cluster_od 606 MB), and the 1 h 56 m / ~70 m / ~11.2 GiB full-run record.

## 2. Key finding — the STRtree hypothesis is disproven

The Batch 0 static audit (B2) hypothesized that per-chunk STRtree rebuild is the
dominant ~70 m Demand bottleneck. **Measurement disproves this:**

`match_points_to_segments_with_distance`, 250 k points × 59,096 segments, median of 3:

| sub-phase | p50 (ms) | % of spatial |
| --- | --- | --- |
| **point_construction** (`[Point(xy) for xy in zip(...)]` + GeoDataFrame) | **1463** | **80.0%** |
| nearest_query (`gpd.sjoin_nearest`) | 323 | 17.7% |
| spatial_index_build (STRtree) | 11.5 | **0.6%** |
| total | 1828 | 100% |

The `sindex_rebuild` counter = 1 per call — the STRtree **is** rebuilt every call
(`seg_view` is a fresh slice each call, so the index is not reused) — but the build
cost is only **0.6%** of spatial matching. **Point construction (the Python
`Point()` list comprehension), not the STRtree, is the spatial bottleneck.**

## 3. Per-phase measured costs (ranked by extrapolated production wall)

Extrapolated to 180 chunks / 46 M rows (pickup+dropoff for spatial):

| Rank | Phase | Per-chunk p50 | Extrapolated production | Evidence |
| --- | --- | --- | --- | --- |
| 1 | **spatial_match** (point 17.5 + nearest 3.9 + sindex 0.04 min) | ~7190 ms (pickup+dropoff) | **~21.6 min** | measured |
| 2 | gzip_write (gzip portion of export) | 626 ms / 250 k rows | ~1.9 min | measured |
| 3 | csv_parse (read 500 k rows of 12 GB CSV) | 573 ms | ~1.7 min | measured |
| 4 | sqlite_append (to_sql 250 k rows) | 444 ms | ~1.3 min (lower bound) | measured, no-PK |
| 5 | service_label_batches (Python loop) | 327 ms / 1 M records | ~0.25 min | measured |
| 6 | od_tensors (build_od_tensors, post DEM-02) | 50 ms / 200 k OD rows | ~0.15 min | measured |
| — | **UNMEASURED SQL passes + POI graph** | — | **~44 min** | gap = 70 − 26 |

**Measured sum ≈ 26 min** vs the ~70 min production wall. The **~44 min unmeasured
gap** is the prime suspect for the true 70 min driver.

## 4. What is in the unmeasured ~44 min gap

GB-scale phases that need a full production run to measure:

- **ordered SQL SELECT** for service labeling (`ORDER BY driver_id, departure_time_ns,
  finish_time_ns, stage_id` over 46 M rows; uses `idx_staged_driver_time`).
- **`build_cluster_od_from_staging`** SQL `GROUP BY` over 46 M rows (uses
  `idx_staged_slot_od`).
- **`export_assigned_orders`** streaming `JOIN` (staged_orders ⋈ service_labels) +
  gzip over 46 M rows (the gzip portion alone is ~1.9 min).
- **POI graph** `build_cluster_poi_graph`: read 413 MB / 1.39 M POI rows + buffer +
  sjoin on 59 k segments.
- per-chunk DataFrame overhead (`.copy()` ×3, `to_numeric`, boolean masks).
- OD tensor materialization (3 × int32 × (13248, 100, 100) = 1.59 GiB).

The SQL passes over 46 M rows are the most likely large contributors. The Python
`service_label_batches` loop itself is only ~15 s, but the SQL ordered scan that
feeds it could be minutes.

## 5. Stage-level + resource summary

| Metric | Value | Source |
| --- | --- | --- |
| Demand wall (production) | ~70 m | run record |
| CPU utilization | single-core (no multiprocessing in `src/`) | static + measured |
| Peak RSS | ~11.2 GiB (production) | run record |
| fs read bytes | ~12.5 GB (12 GB orders + 413 MB POI + 17.8 MB partition + 43 MB relations) | file sizes |
| fs write bytes | ~5 GB outputs (2.69 GiB assigned.gz + 606 MB cluster_od + npz + graph assets) + SQLite staging peak (deleted) | file sizes |
| SQLite peak size | GB-scale (46 M rows × 14 cols) — unmeasured (sandbox-blocked) | — |
| Per-chunk rows | 500,000 (production chunksize) | config |
| Per-chunk degradation | `sqlite_append` expected to slow as the PK autoincrement index grows (mechanism confirmed; magnitude unmeasured at GB scale); `spatial_index_build` is constant across chunks (constant 59 k-segment layer) | measured + static |
| gzip output size / time | 2.69 GiB (assigned.gz); ~1.9 min gzip portion | file size + measured |

## 6. Did the index really get rebuilt / reused? (point 5)

- **Rebuilt every call:** yes. `sindex_rebuild` counter = 1 per
  `match_points_to_segments_with_distance` call (360 calls = 180 chunks × pickup+dropoff).
  `seg_view = segments[["seg_id","geometry"]]` is a fresh GeoDataFrame slice each call,
  so its `.sindex` is built fresh — **the same segment GeoDataFrame's sindex is NOT reused.**
- **But the rebuild cost is 0.6%** (11.5 ms / 1828 ms). Reusing it would save ~4 s
  total across the whole run — not worth the tie-break risk. The audit's emphasis on
  sindex rebuild was misplaced; **Point construction (80%) is the real spatial cost.**
- **Point vs nearest split:** point_construction 1463 ms (80%), nearest_query 323 ms
  (17.7%), sindex 11.5 ms (0.6%). The pre-build-instrumentation split (Batch 1 timing,
  extended in Batch 2A) cleanly separates them.
- **SQLite append vs DB size:** not measurable at GB scale here (the 5 k/120 k runs
  that would populate a meaningful DB are blocked). The `stage_id INTEGER PRIMARY KEY
  AUTOINCREMENT` means appends slow as the index grows; the no-PK benchmark (444 ms /
  250 k rows) is a lower bound.

## 7. Next batch — at most 3 optimization experiments

Ranked by the **measured** results (per the task: do not preset via static analysis):

1. **DEM-P1 — Vectorize Point construction** (shapely/geopandas `points_from_xy` /
   vectorized geometry) instead of the Python `[Point(xy) for xy in zip(...)]`
   comprehension. **Evidence:** 80% of spatial matching, ~17.5 min extrapolated — the
   largest measured cost. **Do NOT pursue STRtree caching** as the primary fix (it is
   0.6%). **Risk:** low (no tie-break / row-order change — same Point geometries);
   verify `sjoin_nearest` output is byte-identical.

2. **DEM-P2 — Run the full production Demand in an unguarded environment** with the
   enhanced timing to quantify the post-staging SQL passes (ordered SELECT / OD
   GROUP BY / export JOIN). **Evidence:** measured phases sum to ~26 min vs 70 min;
   the ~44 min gap is the prime suspect for the true driver and must be confirmed
   before SQLite-side work. **Risk:** none (measurement only).

3. **DEM-P3 — Conditional on DEM-P2:** if the SQL passes dominate, test SQLite
   transaction batching / prepared-statement append / sharded staging; if Point
   construction is confirmed, implement DEM-P1. **Do not optimize `od_tensors`
   further** (0.15 min; DEM-02 already vectorized).

## 8. Honest limitations

- The full production run is sandbox-blocked; per-phase costs are real in-process
  measurements at production-representative scale, **not** a single end-to-end run.
  The aggregate is an extrapolation (per-chunk × 180) for the chunk-loop phases and a
  **gap** (70 − 26) for the GB-scale SQL passes.
- The enhanced profiling timing (per-call P50/P95/max, sindex-rebuild counter,
  SQLite-size sampling, `/proc/self/io` bytes) is in the working tree, env-gated
  (`ROADNET_DEMAND_TIMING`), off by default, and **uncommitted** (per the task's
  "no production code changes" constraint). It is ready to run in an unguarded
  environment to close the 44 min gap.
- The most important correction: **STRtree is not the Demand bottleneck.** Point
  construction is, within spatial matching; and the unmeasured SQL passes may yet
  dominate the full 70 m.
