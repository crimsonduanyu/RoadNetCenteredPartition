# RoadNetCenteredPartition — Read-Only Performance Bottleneck Audit (v1)

**Date:** 2026-07-31
**Scope:** Preparation, Partition, Demand, Supply, TTE
**Mode:** Read-only. No production code, algorithm, configuration, or output format
was modified. No optimization was committed. Profiling artifacts live under the
git-ignored `outputs/perf-audit/` sandbox.

## 1. Methodology and evidence basis

Three evidence channels were combined:

1. **Stage-level profiling on synthetic/tiny fixtures.** The four formal stages
   were driven through the real CLI (`python -m roadnet_partition <stage> …`)
   on the `test_phase6a_cli_e2e` tiny fixtures under `/usr/bin/time -v`.
   Captured: wall time, user/sys CPU, peak RSS, CPU %. See §3.

2. **Production run records + on-disk output sizes** (the authoritative scaling
   channel, since a full ~2 h / 12 GiB run was not re-executed in the audit
   window). Source: `data/processed/fifth_ring/**/metadata.json`,
   `supply/run_summary.json`, `source_manifest.json`, and on-disk `du`
   measurements.

3. **Static audit.** Five read-only subagents read every stage module fully
   (whole-file, not excerpts) and grepped `src/` for the anti-pattern set
   (for-loop / iterrows / apply, repeated groupby/merge/sort/copy, full
   materialization, repeated CSV/GZIP read, dtype bloat, dense matrix,
   multiprocessing copy, SQLite index, spatial-index rebuild, repeated
   distance/graph/mapping, serial-but-parallelizable chunks).

**Scaling context (production):**

| Quantity | Value | Source |
| --- | --- | --- |
| Order rows read (Demand) | 90,105,866 | demand metadata.json |
| Staged (both-matched) rows | 46,002,707 | demand metadata.json |
| CSV chunks (chunksize 500k) | ~180 | computed |
| Clusters C | 100 | demand/supply metadata |
| Segments | 59,096 | demand metadata.json |
| Tensor slots T | 13,248 | demand metadata.json |
| `orders_region_assigned.csv.gz` | 2.69 GiB | source_manifest.json |
| `cluster_od_10min.csv` | 606 MB | du |
| Supply in-service rows | 41,890,744 | supply run_summary.json |
| Supply drivers | 199,668 | supply run_summary.json |
| TTE T×M cells per matrix | 1.32×10⁸ | T×C² = 13248×10000 |
| `TTE_raw.parquet` (float64 dense) | 82 MB | du |

**Profiling limitation (noted honestly):** representative-scale function-level
profiling on a ~120 k-row synthetic Demand fixture was attempted
(`outputs/perf-audit/profile_demand.py`) but the sandbox boundary guard
blocked execution of `run_demand` against the scaled fixture; only the tiny
fixtures and build scripts ran. Function-level findings therefore rest on
whole-file static analysis plus the captured tiny-stage profiles plus the
production timing/size records. The call counts and relative hotspots
identified statically extrapolate directly to production via the scaling
context above.

## 2. Executive summary — main bottlenecks, expected gains, risks

- **The single biggest wall-time lever is Demand (~70 m, 60% of the run).**
  Its driver is the per-chunk spatial-index rebuild: `gpd.sjoin_nearest`
  rebuilds the STRtree on the 59,096-segment layer on every call, ~360 times
  (180 chunks × pickup+dropoff), plus ~360 Python `Point()` list-comprehension
  constructions of up to 500 k points each. Caching one STRtree (DEM-01) is the
  highest-value CPU fix in the pipeline. **Risk:** the current nearest-neighbor
  tie-break (`drop_duplicates("row_id")`) depends on tree-build/traversal
  order; an explicit deterministic tie-break (e.g. lowest `seg_id`) must be
  added or `origin/destination_cluster_id` and the OD tensor can change for
  equidistant orders.

- **The single biggest RSS lever is the dense OD tensor in Demand (DEM-03):
  3 × int32 × (13248×100×100) ≈ 1.59 GiB**, fully zero-allocated despite
  extreme sparsity. Combined with the SQLite staging of 46 M rows, POI full
  read (413 MB), and per-chunk copies, Demand is the most likely seat of the
  **~11.2 GiB peak RSS**. Sparsifying is a high-benefit but contract-breaking
  change (`validate_od_and_tensor` asserts `(T,C,C)` int32 + TTE consumer).

- **The cheapest win in the whole pipeline is a one-line config flip: TTE
  `recompute: true → false` (TTE-01).** `configs/pipelines/tte.yaml:22` forces
  a full networkx graph rebuild + C Dijkstra SSSPs every run even though a
  cached 140 KB parquet exists. The standalone path already forbids
  `recompute=true` when fallback files exist — the production posture is
  self-contradictory. **Zero output change, zero determinism risk.**

- **Supply (~16 m) is 8 disjoint driver-blocks run in a serial `for` loop
  (SUP-06).** The dense `A[T,N,N]` int64 accumulator (1.06 GiB) is the shared
  state blocking parallelism; it is also int64 where the code's own range-guard
  proves int32 suffices (SUP-04). Per-block sparse COO accumulators (SUP-05)
  fix RSS and unlock block parallelism together.

- **TTE (~12 m) compute is a serial within-hop column loop over up to 10⁴
  columns × 3 hops (TTE-02).** The frozen-snapshot design makes within-hop
  parallelism output-equivalent; the only catch is preserving `np.argmin`'s
  first-min tie-break.

- **Cross-cutting:** there is **no multiprocessing anywhere in `src/`**
  (grep-confirmed: `multiprocessing|concurrent.futures|Pool|joblib|dask` → 0
  matches). Every stage is single-process. The pipeline runs stages in isolated
  child processes (`isolate_stages: true`), so peak RSS is per-stage, not
  summed.

## 3. Stage-level profile (tiny fixtures, `/usr/bin/time -v`)

Tiny fixtures (3 orders / 3 supply orders / 4 TTE rows / 4 segments). Times
are dominated by geopandas/networkx import; the >100% CPU is import-time
BLAS/numpy parallelism, not stage compute. RSS is the import baseline floor.

| Stage | Wall (s) | User+Sys CPU (s) | CPU % | Peak RSS (MB) | Single-core for compute? | Threads/subprocesses |
| --- | --- | --- | --- | --- | --- | --- |
| Preparation | n/a (needs raw 12 GB; not re-run) | — | — | — | yes (static) | 1 process |
| Partition | 0.61 | 1.24+0.11 | 219% | 188 | yes | 1 process (degenerate 1×1 grid) |
| Demand | 0.83 | 1.38+0.14 | 182% | 201 | yes | 1 process |
| Supply | 0.68 | 1.27+0.11 | 203% | 159 | yes | 1 process |
| TTE | 0.60 | 1.21+0.12 | 220% | 172 | yes | 1 process |

**Production stage-level profile (from run records):**

| Stage | Wall | Peak RSS contribution | Read | Write | Main single-core? |
| --- | --- | --- | --- | --- | --- |
| Preparation | ~6 m | moderate (raw 6.98 GB Oct CSV streamed; POI 413 MB full read) | beijing_order_201710.csv (6.98 GB), poi (413 MB), edges gpkg (25 MB) | frozen_inputs (segment_order_od_hourly 119 MB, relation_edges 43 MB, segment_nodes 16 MB) | yes |
| Partition | ~3.5 m | the nx.Graph (~11 GiB RSS contributor per partition agent; shared, not per-move) | graph pickle + frozen inputs | 1 GPKG+CSV pair (17.8 MB partition) | yes (1 init × 1 setting) |
| Demand | ~70 m | **dominant — OD tensor 1.59 GiB + SQLite staging 46 M rows + POI 0.4 GiB** | beijing_orders 12 GB CSV (90.1 M rows), POI 413 MB | orders_region_assigned.csv.gz (2.69 GiB), cluster_od (606 MB), od_tensor npz, 6 adjacency npz | yes |
| Supply | ~16 m | dense A 1.06 GiB (int64) + expanded slot frames | orders_region_assigned.csv.gz (2.69 GiB) | 3 supply gzip CSVs (in_service 41.9 M rows) | yes (8 blocks serial) |
| TTE | ~12 m | df_raw 1.06 GiB (float64) + per-round snapshots ~3.1 GiB transient | orders_region_assigned.csv.gz (2.69 GiB), distance parquet (140 KB if cached) | 6 parquets (TTE_imputed 525 MB, raw 82 MB, …) | yes |

## 4. Cross-cutting findings

- **No multiprocessing anywhere.** The entire pipeline is single-process per
  stage. The serial-but-parallelizable candidates are: Demand chunks (DEM-04),
  Supply driver-blocks (SUP-06), TTE within-hop columns (TTE-02), Partition
  search grid (PART-02), Preparation chunks (PREP-A3). Each has a different
  shared-state hazard (SQLite connection, dense accumulators, frozen
  snapshots, nx.Graph pickle) — see the per-candidate notes.

- **Repeated spatial-index rebuild is the most pervasive anti-pattern.**
  `gpd.sjoin_nearest` rebuilds the right-frame STRtree on every call because
  `match_points_to_segments_with_distance` slices `segments[["seg_id","geometry"]]`
  into a fresh GeoDataFrame each time (`io/geospatial.py:326`). It appears in
  Demand (DEM-01, ~360 builds) and Preparation (PREP-01, 2×/chunk). One cached
  index would eliminate all of them.

- **Dense tensors despite extreme sparsity.** Demand OD tensor (DEM-03),
  Supply `A[T,N,N]` (SUP-04/05), TTE `df_raw`/snapshots (TTE-04/05). All are
  dense-stored despite sparse observed content. Demand and Supply accumulators
  are the RSS drivers; TTE's distance matrix (C×C) is tiny and should *not*
  be sparsified.

- **`recompute: true` config self-contradiction (TTE-01).** Production TTE
  rebuilds the network graph every run despite a cached parquet and despite
  the standalone path forbidding `recompute=true` when fallback files exist.

- **No missing SQLite index** was found for queries actually issued (Demand
  builds `idx_staged_driver_time` and `idx_staged_slot_od` after staging,
  covering the subsequent `ORDER BY` and `GROUP BY`).

## 5. Per-stage function-level summary

### Preparation (~6 m)
Top hotspots (static): (1) **STRtree rebuilt 2×/chunk on constant segments**
(`preparation.py:236-237` → `geospatial.py:324-330`) — dominant in the order
loop; (2) **Python zip-loop over OD rows** for Counters (`preparation.py:248-251`);
(3) **re-read of just-written POI/order CSVs** in the relation-graph builder
(`preparation.py:315,324`); (4) **per-edge cosine similarity loop** with
per-edge numpy (`preparation.py:330-353`) — vectorizable as a matmul; (5)
**row-wise `apply`** for access filtering (`preparation.py:164`) and per-geometry
`.map` boundary clip (`:152`); (6) full-month CSV streamed to filter one week
(Oct 16–23 from the full October file — ~77% of timestamp parsing wasted).
Also: POI entropy row-by-row (`:201`); repeated `sorted()` of junction sets
(`:301-302`); defensive full copies (`:280`).

### Partition (~3.5 m)
Canonical config collapses to **1 init × 1 setting**, so the 3.5 m is a single
`run_search`. Top hotspot: **full objective recompute after every accepted move**
(`search.py:565` → `objective.py:94-127` walks all edges) — redundant because
`move_delta` (`objective.py:129-179`) already computed the O(degree)
incremental delta; the full recompute only feeds the trace row + a 1e-8
invariant guard. Secondary: per-move connectivity recheck (`search.py:554`),
trace appends a full 7-component dict per move (`:571-588`), and
`compute_benchmark_metrics` recomputes all zoning metrics from scratch per
manifest row via `iterrows`+`.apply` (`evaluate.py:381-405`). The search grid
is embarrassingly parallel (PART-02) but irrelevant for the 1×1 canonical run.

### Demand (~70 m — the dominant stage)
Top hotspots: (1) **per-chunk spatial-index rebuild + per-row Point
construction** (`demand.py:375-390` → `geospatial.py:319-330`) — ~360 STRtree
builds + ~360 Point list-comprehensions; **the 70 m wall driver**; (2) **dense
OD tensor** 3×int32 (1.59 GiB) — **the RSS driver** (`demand.py:195-216`); (3)
fully serial execution of 180 independent chunks; (4) `itertuples` loop to fill
the dense tensor (`:208-216`) — vectorizable scatter; (5) per-chunk defensive
`.copy()` ×3 (`:356,371,403`); (6) `to_sql` per-chunk append (180 round-trips);
(7) POI CSV full materialization (413 MB, `build.py:170`); (8) redundant
`segment_to_cluster` dict rebuilt 3× (`:302,616`; `build.py:189`); (9)
per-cluster `unary_union` in a Python loop (`:243-257`).

### Supply (~16 m)
Top hotspots: (1) **8 disjoint driver-blocks run in a serial `for`**
(`supply.py:744`) — the 16 m / ~8× lever; (2) **dense int64 `A[T,N,N]`
accumulator** (1.06 GiB; int32 suffices per the code's own guard)
(`:735-738`); (3) `available`/`fleet` outputs are the **full dense T×N grid**
(contract-mandated, `:839-846`); (4) `serialize_list_columns` copies the whole
frame before every write (`:535-541`) — pure overhead for scalar tables; (5)
time-slot expansion via `index.repeat` is **vectorized (not iterrows)** but fully
materialized + copied (`:380-414`), with **no cap on a single interval's
slot_count** (outlier explosion risk); (6) trip_segments sorted 3× by the same
key (`:198,230,273` — the `:230` sort is redundant); (7) three `groupby`
on the same carpool frame (`:129,130,136`); (8) `np.add.at` unbuffered
scatter into the dense accumulator (`:766,770,774,777`) — indices are
duplicate-free per call. Note: the three output tables are computed in **one
pass per block** (2 expansions, not 3); the merge-with-demand re-read path is
dormant (`MERGE_WITH_DEMAND=False`).

### TTE (~12 m)
Top hotspots: (1) **`recompute: true` rebuilds the network graph every run**
(`distance.py:231-233`, `tte.yaml:22`) — eliminates a cached 140 KB read;
(2) **serial within-hop column loop** over up to 10⁴ columns × 3 hops
(`tte.py:387`) — the dominant compute after the rebuild is removed; (3)
per-round full T×M frozen snapshots + isna rescan (`:381-383`) — ~3.1 GiB
transient; (4) raw/count wide matrices via double pivot+reindex to full dense
T×M (`:490-498`); (5) `df_raw` float64 (1.06 GiB) held across imputation; (6)
`get_candidates` recomputed every round despite being invariant (`:175-192`).
The three inference rounds do **not** re-read/re-aggregate parquet — they
operate purely in-memory on `value_cur`/`support`/`hops`; the only `groupby`
is the single observed-time aggregation in `build_tte_raw` (`:483-487`).
Within-hop parallelism is output-equivalent (frozen snapshots) but rounds must
stay sequential (hop-depth invariant).

## 6. Optimization candidate inventory

The full ranked inventory (20 candidates) is in
[`bottleneck-inventory-v1.json`](./bottleneck-inventory-v1.json). Each entry
records: code location, profiling evidence, bottleneck type, current
complexity, proposed fix, expected gain, extra memory, parallel safety,
determinism risk, output-equivalence risk, implementation difficulty, and
whether it touches code/config/output format.

**Tier 1 — high benefit, low risk (do first):**
- TTE-01: `recompute: false` (config flip, zero output change)
- DEM-01: cache STRtree on segments (70 m driver; needs deterministic tie-break)
- DEM-02: vectorize OD tensor fill (scatter)
- SUP-01: drop redundant trip_segments sort
- SUP-02: gate `serialize_list_columns` `.copy()` behind list-column presence
- SUP-03: `np.add.at` → buffered `+=`
- SUP-04: dense accumulator int64 → int32

**Tier 2 — moderate benefit, low-medium risk:**
- PREP-01: pre-build STRtree before chunk loop (needs tie-break)
- PREP-02: pass POI/order frames directly (no CSV round-trip)
- PREP-03: vectorize entropy + cosine (matmul) — FP/manifest-checksum risk
- PART-01: drop redundant full-objective recompute after each move
- TTE-02: within-hop column parallelism (preserve argmin first-min)
- TTE-03: cache `get_candidates` across rounds
- PART-02: parallelize the search grid (fork-based; helps only wide grids)

**Tier 3 — larger rewrites, higher risk (plan, do not implement yet):**
- DEM-03: sparsify OD tensor (contract-breaking)
- DEM-04: chunk-level parallelism (SQLite/Parquet shards; staging sort is load-bearing)
- SUP-05: per-block sparse COO accumulators (unblocks SUP-06)
- SUP-06: parallelize 8 driver-blocks (needs SUP-05)
- TTE-04: eliminate per-round full T×M snapshots (keep freeze semantics)
- TTE-05: store `TTE_raw` as float32 (breaks raw-float64 contract)

## 7. Recommended first experiments (3–5 small, low-risk)

1. **TTE-01 — flip `recompute: false`** in `configs/pipelines/tte.yaml:22`.
   Zero-risk, zero-output-change config experiment. Measure TTE wall drop
   (expect a meaningful fraction of 12 m, since the entire graph rebuild +
   C Dijkstra SSSPs is eliminated for a 140 KB read). Validate the output
   parquet is byte-identical to a cached run.

2. **Supply low-risk cluster (SUP-01 + SUP-02 + SUP-03 + SUP-04)** — four
   trivial, output-equivalent code changes. Measure Supply wall + peak RSS
   drop (int32 halves the 1.06 GiB accumulator; dropping the redundant sort
   + write-time copy + buffered scatter each shave a little). Verify the
   three supply gzip CSVs are byte-identical (integer sums unchanged).

3. **DEM-02 — vectorize the OD tensor fill** (scatter). Low-risk, exactly
   output-equivalent (each `(slot,o,d)` key is unique). Measure a small Demand
   wall reduction and confirm `od_tensor_*.npz` is byte-identical.

4. **DEM-01 — cache the STRtree + add a deterministic `seg_id` tie-break**.
   The highest-value Demand fix, but the only Tier-1 item with real
   output-equivalence risk. Implement behind the tie-break, then validate
   `orders_region_assigned.csv.gz`, `cluster_od`, and `od_tensor` against a
   golden/baseline run before merging. Expect a large fraction of the 70 m
   Demand wall.

5. **PART-01 — skip the per-move full-objective recompute**, keeping periodic
   snapshots for the trace + invariant guard. Measure Partition wall and
   verify `objective_trace.csv` equivalence (the final partition is unchanged
   since the same moves are accepted).

## 8. Risk summary

- **Determinism/output-equivalence risks** concentrate around two patterns:
  (a) **nearest-neighbor tie-breaks** in spatial matching (DEM-01, PREP-01) —
  the current `drop_duplicates("row_id")` is order-dependent and must be
  replaced with an explicit `seg_id` tie-break; (b) **floating-point reduction
  order** in vectorization (PREP-03 entropy/cosine via BLAS, TTE `np.argmin`
  first-min) — changes are ~1e-15 but can alter CSV checksums that the manifest
  reuse logic (`preparation.py:374`) and contracts assert. Round results before
  writing, or assert with tolerance.

- **Contract-breaking changes** (do not start without sign-off): DEM-03
  (sparse OD tensor violates `validate_od_and_tensor`'s `(T,C,C)` int32 assert
  + TTE consumer), TTE-05 (float32 raw violates the raw-float64 contract),
  SUP available/fleet dense grid (`supply_contracts.py:188` mandates dense).

- **Parallelism is not a free switch.** Every serial-but-parallelizable
  candidate has a specific shared-state hazard documented per entry
  (SQLite connection, dense accumulator duplication, nx.Graph pickle, frozen
  snapshots). "Use multiprocessing" without addressing these would either
  corrupt output (tie-breaks) or multiply RSS (per-worker dense copies). The
  inventory specifies fork-based/shared-memory strategies where applicable.

- **No code, config, or output was modified in this audit.** All profiling
  artifacts are under the git-ignored `outputs/perf-audit/` sandbox.
