# Demand Full-Production Profile (v1) — Batch 2B (MEASURED)

**Date:** 2026-08-01
**Status:** COMPLETE. A full production Demand run was executed successfully with
`ROADNET_DEMAND_TIMING=1` on the raw-only pipeline. The Batch 2A 44 min gap is now
fully decomposed into measured sub-phases. No optimization was implemented; no SQLite
schema/index/PRAGMA, parallelism, `points_from_xy`, STRtree caching, tie-break, or
output-contract change was made.

## 1. Run

- **Repo:** the official RoadNetCenteredPartition checkout (verified); HEAD `bc2d040`.
- **Run-id:** `demand-prof-2b` — fresh raw-only pipeline, `--to-stage demand --isolate-stages`,
  no resume, no canonical/artifacts. Demand contract passed; all formal outputs present.
- **Scale:** 90,105,866 rows read → 46,002,707 staged (both-matched); 30,651,791 exclusive +
  15,350,916 carpool; 100 clusters, 59,096 segments, 13,248 tensor slots; 1,389,470 POI valid
  (665,538 matched); 181 chunks (180×500k + 1×105,866).

## 2. Stage-level + resources

| Metric | Value |
| --- | --- |
| Demand wall | **69.85 min** (4190.9 s) |
| Pipeline wall (prep+partition+demand) | 90.2 min (5413 s) |
| User / sys time | 5187.9 s / 229.75 s |
| CPU utilization | 99% (single-core; no multiprocessing in `src/`) |
| Peak RSS | **11.06 GiB** |
| SQLite staging peak file | 11.83 GiB (46 M rows + 2 indexes + PK) |
| proc_io read / write bytes | 5.63 GB / 26.6 GB |
| Swaps / major faults | 0 / 0 |

## 3. Completeness (item 三.3)

- Sub-phase sum: **67.45 min** (4047.1 s)
- Unclassified: **143.8 s = 3.43%** of the Demand wall
- Target ≤3%: **miss** (3.43%); Max ≤5%: **MET** (3.43% ≤ 5%)
- The unclassified 3.43% is identified as: partition load (`gpd.read_file` 17.8 MB +
  `to_crs`) + `build_cluster_index` (per-cluster `unary_union`) + interpreter imports
  (geopandas/networkx/numpy) + metadata write + misc. **No SQL/query time is hidden.**
  Since unclassified < 5%, optimization conclusions are permitted.

## 4. Sub-phase ranking by measured wall (replaces the 44 min gap)

| # | Phase | calls | total | category |
| --- | --- | --- | --- | --- |
| 1 | **export_csv_gzip** | 461 | **12.7 min** | assigned-orders export |
| 2 | dropoff_nearest_query | 181 | 9.0 min | spatial matching |
| 3 | pickup_point_construction | 181 | 8.7 min | spatial matching |
| 4 | pickup_nearest_query | 181 | 8.7 min | spatial matching |
| 5 | dropoff_point_construction | 181 | 8.6 min | spatial matching |
| 6 | service_label_compute | 1 | 4.4 min | service labeling |
| 7 | service_labels_insert | 4601 | 3.8 min | service labeling |
| 8 | export_join_fetch | 462 | 2.7 min | assigned-orders export |
| 9 | csv_parse | 182 | 2.2 min | CSV |
| 10 | sqlite_append | 181 | 2.1 min | SQLite staging |
| 11 | od_groupby_fetch | 1 | 1.2 min | cluster OD |
| 12 | create_index_driver_time | 1 | 0.57 min | SQLite index build |
| 13 | create_index_slot_od | 1 | 0.53 min | SQLite index build |
| 14 | od_pivot | 1 | 0.34 min | cluster OD |
| 15 | segment_mapping | 181 | 0.33 min | spatial matching |
| 16 | other_writes (graph assets) | 1 | 0.30 min | graph assets |
| 17 | npz_write | 1 | 0.29 min | cluster OD |
| 18 | service_labels_create_index | 1 | 0.24 min | service labeling |
| 19 | od_csv | 1 | 0.22 min | cluster OD |
| 20 | point_construction (POI) | 1 | 0.14 min | POI graph |
| 21 | tensor_alloc_fill | 1 | 0.13 min | cluster OD |
| 22 | nearest_query (POI) | 1 | 0.11 min | POI graph |
| 23 | pickup/dropoff_spatial_index_build | 181×2 | 0.07 min | spatial matching (STRtree) |
| 24-29 | service_labels_commit/create_table, ordered_select_execute, sqlite_commit | — | ~0 | — |

**By category:** spatial matching 35.1 min (52%) · assigned-orders export 15.4 min (23%) ·
service labeling 8.5 min (13%) · csv_parse 2.2 · sqlite_append 2.1 · cluster OD 2.1 ·
SQLite index build 1.1 · POI graph 0.24 · graph assets 0.30 · unclassified 2.4 (3.4%).

## 5. Decomposition of the Batch 2A 44 min gap (now measured)

| Component | Min |
| --- | --- |
| Underestimated spatial (nearest_query: 2A est. 3.9 → real 17.7) | 13.8 |
| Assigned-orders export (gzip 12.7 + join 2.7) | 15.4 |
| Service labeling (compute 4.4 + insert 3.8 + index 0.24) | 8.5 |
| Cluster OD (groupby + pivot + csv + tensor + npz) | 2.1 |
| SQLite index build | 1.1 |
| POI graph + other writes | 0.5 |
| Unclassified overhead | 2.4 |
| **Total** | **43.8** (≈ 44) |

The gap is fully accounted for: ~14 min was a Batch 2A **under-estimate** of
`nearest_query` (synthetic random points had 9% match → cheap nearest; production
53% match → heavy nearest), and ~28 min was the SQL/gzip passes now measured.

## 6. Key revisions to Batch 2A

1. **STRtree rebuild is still negligible** — 4.0 s total (0.07%) across 362 builds
   (~0.011 s each). Confirmed NOT the bottleneck. STRtree caching is pointless.
2. **Point construction is NOT 80% of spatial** — real production shows
   `point_construction` 17.4 min ≈ `nearest_query` 17.7 min (nearly equal). Batch 2A's
   synthetic benchmark undercounted nearest_query (3.9 → 17.7 min).
3. **The #1 single phase is `export_csv_gzip`** (12.7 min, 19%) — it was inside
   Batch 2A's "unmeasured gap".

## 7. DEM-P1 max savable (item 七)

DEM-P1 (vectorize Point construction) ceiling = **17.4 min** (point_construction total),
realistic **~15–16 min** (vectorized `points_from_xy` still costs ~1–2 min). Caveat: it is
only ~25% of the Demand wall; `nearest_query` (17.7) and `export_csv_gzip` (12.7) remain.
Batch 2A's "point construction is the spatial bottleneck" framing is revised to
"point ≈ nearest".

## 8. Chunk degradation + SQLite analysis (item 五/六)

- **Degradation is minimal.** p95/p50 across 181 chunks: csv_parse 1.05, sqlite_append 1.04,
  pickup_nearest_query 1.03, pickup_point_construction 1.05, export_join_fetch 1.04,
  export_csv_gzip 1.02. No late-chunk blow-up.
- **sqlite_append** slows ~4% (p95/p50=1.04) — slight, from `stage_id` AUTOINCREMENT PK
  index growth; not a bottleneck.
- **CREATE INDEX:** driver_time 34.4 s, slot_od 32.0 s, service_labels 14.3 s — separate,
  not hidden in "unclassified".
- **ordered SELECT:** uses `idx_staged_driver_time` as a **covering index scan** (EXPLAIN
  confirmed) — no table scan.
- **SQLite PRAGMA (defaults, unmodified):** journal_mode=delete, synchronous=2 (FULL),
  temp_store=0, cache_size=-2000 (2 MB), page_size=4096, mmap_size=0, automatic_index=1.
  `synchronous=FULL` + `journal_mode=delete` → fsync per commit → contributes to the 26.6 GB
  write amplification. (No PRAGMA was changed.)
- **Instrumentation gap:** EXPLAIN for the cluster-OD GROUP BY and the export JOIN returned
  "no such table: service_labels" — evidence was collected after staging but before
  `label_staged_service_types` created the table. The run DB is now deleted
  (`keep_staging_db=false`); cannot re-collect. The ordered-SELECT plan IS captured.

## 9. Batch 2C candidates (3, ranked by measured wall)

1. **2C-1 — Assigned-orders export (15.4 min, 23%).** `export_csv_gzip` is the #1 single
   phase (12.7 min). Test internal pipelining (stream JOIN→CSV→gzip without per-chunk full
   materialization) + compression-level tuning, **keeping the `.csv.gz` format + byte-identity**.
   Lowest determinism risk; in-scope (gzip pipelining was explicitly allowed in the Batch 2A
   decision tree).
2. **2C-2 — Point construction vectorization (17.4 min, 26%) — DEM-P1.** Use `points_from_xy`
   instead of the Python list comprehension. Ceiling ~15–16 min. Low risk (no tie-break/row-order
   change). Revised: point ≈ nearest (not 80%); vectorizing point leaves `nearest_query`
   (17.7 min) untouched. Subject to Batch 2C scope (`points_from_xy` was prohibited for 2B
   implementation).
3. **2C-3 — Spatial nearest_query (17.7 min, 26%).** The nearest-neighbor **query** is the
   cost — sindex rebuild is negligible (4 s, 0.07%), so STRtree caching is NOT the fix.
   Investigate the `sjoin_nearest` query path. Higher risk (must preserve nearest-neighbor
   tie-break + output equivalence). Subject to Batch 2C scope.

## 10. What was / was not done

**Done:** executed the full production Demand run (run-id `demand-prof-2b`, exit 0, contract
passed); measured all 26 sub-phases; sub-phase sum 67.45 min vs Demand wall 69.85 min
(unclassified 3.43%, within the 5% max); decomposed the Batch 2A 44 min gap; revised the
Batch 2A conclusions (point ≈ nearest; export_csv_gzip is #1; STRtree still negligible).

**Not done:** no optimization implemented; no SQLite PRAGMA/schema/index change, no sharding,
no parallelism, no `points_from_xy`, no STRtree caching, no tie-break change, no sparse
tensor, no output-contract change. Did not enter Batch 2C. Did not operate the `sanitized`
directory.
