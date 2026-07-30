# Trip-Time (TTE) Preprocessing Pipeline — Method Record (Stage 4)

> Internal method/experiment record for thesis reference. Every quantitative claim is
> traceable to a real product, a code location, or a logged experiment run in this repo.
> Numbers that could not be verified are marked **[待核]**. KoopmanTTE is referenced for
> pipeline *parity* only (its code is not reproduced here).

---

## 1. Problem and goal

KoopmanTTE builds a trip-time dataset by aggregating ride orders into a `(time slot × OD)`
matrix of trip times and filling the (heavily missing) matrix by transitive imputation,
where the spatial unit is a **regular grid zone**. The goal here is to migrate that
preprocessing into `RoadNetCenteredPartition` with two substantive changes:

1. The spatial unit becomes a **road-network semantic cluster** (the canonical 100-cluster
   partition) instead of a regular grid.
2. The imputation's spatial pruning is made **road-network-topology aware** (cluster
   representative-point shortest-path distance instead of straight-line Haversine), and the
   transitive imputation is generalized from single-hop to **bounded multi-hop** with an
   **edge-level observation gate** and **provenance** outputs.

The pipeline runs as **Stage 4** of `roadnet-partition run`, depending only on
Stage 2 (demand) products.

## 2. Data and products

**Inputs** (both Stage 2 `order_pipeline` products):
- `orders_region_assigned.csv.gz` — one row per assigned order; columns used:
  `departure_time`, `finish_time`, `origin_cluster_id`, `destination_cluster_id`
  (`roadnet_partition.downstream.tte`,
  `DEPARTURE_COL`/`FINISH_COL`/`ORIGIN_COL`/`DESTINATION_COL`).
  `trip_time = finish_time − departure_time` (minutes).
- `cluster_index.csv` — per-cluster `centroid_x/y` (EPSG:32650) and `centroid_lon/lat`
  (EPSG:4326), used for representative-node selection.

**Products** (under `data/processed/fifth_ring/tte/`; all matrices `13248 × 10000` OD columns
plus a datetime index column, i.e. `13248 × 10001` on disk):

| Product | dtype | size | Meaning |
|---|---|---|---|
| `TTE_raw.parquet` | float64 | 85.8 MB | observed median trip time per `(slot, OD)`; NaN where unobserved |
| `TTE_count.parquet` | int32 | 25.2 MB | number of post-filter trips supporting each cell; `count≥1 ⟺ observed` |
| `TTE_imputed.parquet` | float32 | 549.6 MB | matrix after multi-hop transitive imputation |
| `TTE_hops.parquet` | int16 | 27.5 MB | provenance: `0`=observed, `r`=filled at hop `r`, `-1`=unfilled |
| `TTE_support.parquet` | int32 | 55.5 MB | provenance: inferred cells = weakest support (min obs count on path); `-1` elsewhere |
| `cluster_network_distance.parquet` | float64 (100×100) | 0.1 MB | cluster representative-point network distance (metres) |
| `cluster_representative_nodes.csv` | — (100 rows) | — | `cluster_id, rep_osmid, dist_to_centroid_m` |

Grid: `T = 13248` ten-minute slots (2017-06-01 .. 2017-08-31), `M = 10000` OD pairs over the
100 clusters (`num_clusters=100`, `num_od_columns=10000`, `num_slots=13248` — Stage 4 summary).

**Raw baseline** (independent of distance source / k): `TTE_raw` NaN ratio **0.8510**;
observed cells **19,742,327** of 132,480,000 (`num_observed_cells`, Stage 4 summary).

## 3. Method — three stages

Each step is organized as *problem → trade-off → choice → evidence*.

### 3.1 Stage A — baseline migration (Haversine + single hop)

- **Problem.** Reuse KoopmanTTE's `functions_dp` transitive imputation on clusters rather
  than grids, consuming Stage 2 products directly.
- **Choices.**
  - **String cluster ids throughout.** OD column names are `"o->d"` parsed by `split("->")`
    with no `int()` cast (`get_transitive_data`, `vectorize_transitive_impute`), so arbitrary
    cluster labels are supported. `SpatialPruner` coerces ids to `str`.
  - **Single-hop transitivity.** For target column `O→D`, estimate via each corridor
    intermediate `k`: `T_od(t) = T_ok(t) + T_kd(t + T_ok(t))`, linear interpolation of `k→D`
    at the arrival time (`calculate_transitive_time`; out-of-axis arrivals → NaN via
    `np.interp(left=nan, right=nan)`); aggregate over corridors by `np.nanmin`
    (`vectorize_transitive_impute`). The min is the tightest upper bound and is the
    quantity the downstream hinge training relies on.
  - **Diagonal `T_kk` (o==d) never imputed.** `get_candidates` returns `[]` when
    `dist_od < 0.01`, which holds on the diagonal (distance 0), so diagonal columns are kept
    as observations and never filled.
  - **Only-fill-NaN invariant.** `mask_fill = isnan(value) & ~isnan(estimate)` — observed
    values are never overwritten (`run_imputation_pipeline`).
- **Evidence.** Unit tests `test_imputation_fills_only_missing_cells`,
  `test_imputation_invariants_under_network_pruner` (`tests/test_tte_dataset.py`).

### 3.2 Stage B — network-distance pruning (replace Haversine)

- **Problem.** Haversine corridor pruning ignores road topology and makes systematic errors
  (e.g. two clusters across a river are close in straight line but require a long detour over
  a bridge), distorting the detour admissibility test.
- **Trade-offs / choices.**
  - **Cluster distance = network shortest path between representative points**, not
    boundary-to-boundary minimum. A boundary min only captures the closest touching points of
    two clusters and systematically understates the distance to the "boundary gap"; it does
    not represent the *typical* O→D travel distance. The representative-point distance carries
    each cluster's own scale and is the typical-OD distance.
  - **Representative node = the cluster's OSM node nearest its centroid** (projected
    Euclidean, EPSG:32650; ties → smallest osmid). A bare centroid may not lie on any road
    node or even inside the cluster (`network_distance.pick_representatives`).
  - **Edge weight = OSM edge `length` (metres)**, not free-flow time (maxspeed coverage is
    only ~0.8% on the segments — insufficient).
  - **Two-layer graph.** Distances are computed on the *filtered* drivable subgraph first;
    cross-component pairs fall back to the *full* OSM graph (never to Haversine). The filtered
    layer is an edge-induced subgraph of the raw OSM junction graph, inheriting the same
    `length` definition (`network_distance.build_filtered_subgraph`).
  - **Undirected / symmetric.** OSM one-way splitting is not a clean direction signal, so the
    graph is undirected (parallel/bidirectional edges collapsed to the minimum length;
    `collapse_min_undirected`); the matrix is symmetrized.
  - The `SpatialPruner` distance API is unchanged; only the distance source is swapped via
    `SpatialPruner.from_distance_matrix` (matrix consumed in km: the metre matrix is divided
    by 1000 in `run_from_config`, matching the validate-speed/min-dist conventions).
- **Evidence (real build).** Raw OSM graph: 27,759 nodes / 41,245 undirected edges, single
  connected component. Filtered layer: 27,741 nodes / 41,165 edges, all lengths inherited.
  Matrix `100×100`: symmetric (`max|D−Dᵀ| = 0`), zero diagonal, all finite (no inf/NaN).
  Off-diagonal distances: min 1056.5 m / median 15481.8 m / max 38018.0 m. The 261 adjacent
  cluster pairs (`cluster_graph_road_edges.csv`): min 1056.5 / median 4108.3 / max 19017.4 m,
  with **0 pairs below 50 m** (not ≈0). Network ≥ Haversine on the same representative nodes:
  **0 violations out of 4950 pairs**. Unit tests in `tests/test_network_distance.py`.

### 3.3 Stage C — multi-hop TDSP + edge gate + provenance

- **Problem.** Single hop leaves many cells unreachable; but naively compounding more hops
  amplifies the *optimizer's curse*: `np.nanmin` over corridor candidates selects the most
  downward-biased (noisiest) estimate, and chaining selected mins across hops compounds the
  downward bias, producing invalid (too-low) "upper bounds".
- **Choices.**
  - **Multi-hop = min-plus iteration to a hop cap.** Each round reads a *frozen snapshot* of
    the previous round's filled matrix as sources, so hop depth equals the iteration round.
    Early-stops when a round fills nothing (`run_imputation_pipeline`).
  - **`max_hops = 3` as a regularizer**, not only a compute cap: it limits how many layers of
    min-selection compound. (Config: `stage4_tte.imputation.max_hops`.)
  - **Edge-level source gate `k` (`source_min_count`).** An *observed* cell may serve as a
    transitive *leg* only if `count ≥ k`; *inferred* cells may serve as legs with no count gate.
    The gate acts on **sources only, never on targets**: thin observations (`1 ≤ count < k`)
    are excluded from the source matrix (`source = value.mask(thin_observed)`) but kept
    verbatim in the output and stay `hops==0`. The source snapshot and the fill base are kept
    as independent objects so a masked thin source is never written back.
  - **Provenance.** `TTE_hops` records the round at which each cell was filled.
    `TTE_support` records the path's weakest support = the minimum observation `count` along
    the chosen chain, recorded for the candidate actually selected by `np.argmin` (so support
    is aligned with the chosen min estimate; `k→D` leg support gathered at the nearest arrival
    slot). Internal `value` is float32 (peak memory ≈ 2 GB).
  - **Confidence weighting is downstream.** This repo emits the signals only; the MAE-vs-hinge
    split and confidence-decayed hinge weighting live in KoopmanTTE training (`models/proposal.py`:
    `mae_loss` for observed, `squared_upper_bound_hinge_loss` for the imputed upper bound),
    which is not modified here.
- **Evidence.** Unit tests: 2-hop correctness + provenance, hop cap, k-gate (thin leg unused),
  thin-observed protection, early stop, argmin-aligned support
  (`tests/test_tte_dataset.py`, the multi-hop section). Full suite: **44 passed**.

## 4. Key experiments and trade-offs

### 4.1 Source gate `k`: coverage vs. bound quality (real runs, full data)

Two full Stage 4 runs (`max_hops=3`), identical except for `k`. The held-out experiment masks
a fixed 5% of observed cells (`987,116` cells, seed 0), re-imputes, and compares the inferred
"upper bound" against the held-out truth.

| Metric | k=3 | k=1 (current) | Source |
|---|---|---|---|
| imputed NaN ratio | 0.4913 | **0.2315** | Stage 4 summary |
| inferred cells | 47,653,554 (35.97%) | 82,071,287 (61.95%) | Stage 4 summary / hops matrix |
| hop1 / hop2 / hop3 fills | 30.64M / 16.47M / 0.54M | 79.60M / 2.09M / 0.37M | Stage 4 summary |
| support min / median / mean | 3 / 4.0 / 4.6 | 1 / 1.0 / 2.0 | held-out verify scripts |
| support `<5` / `<10` share | 0.657 / 0.954 | 0.918 / 0.987 | held-out verify scripts |
| held-out fill rate | 0.584 | 0.714 | held-out experiment |
| **bound violation rate** (pred < obs) | 0.1541 | **0.2290** | held-out experiment |
| pred−obs: mean / median | +10.12 / +10.30 | +6.52 / +7.23 | held-out experiment |
| pred−obs: p1 / p0.1 (left tail) | −19.53 / −32.02 | −23.42 / −37.37 | held-out experiment |
| underestimate on violations: median / p95 / max | 5.30 / 21.08 / 56.96 | 5.50 / 22.51 / 56.82 | held-out experiment |
| violation @ hop1 / hop2 / hop3 | — | 0.230 / 0.014 / 0.002 | held-out (k=1) |

Reading: lowering k from 3 to 1 raises coverage substantially (imputed NaN 0.49→0.23; held-out
fill 0.58→0.71) at the cost of bound quality (violation rate 0.154→0.229; support median 4→1,
i.e. most fills now rest on a *single* supporting observation). The per-violation *magnitude*
is similar (median ≈5–5.5 min, max ≈57 min); it is the violation *rate* that rises. At k=1 the
multi-hop barely contributes (hop1 = 97% of fills) and hop2/3 fills carry very low violation
rates — the curse is dominated by single-hop nanmin over many corridors.

### 4.2 Coverage diagnosis (read-only, no imputation rerun)

Decomposition of the k=3 NaN (0.4913), via same-slot corridor reachability over
`TTE_count` + the network distance matrix (a *conservative* lower bound on reachability, since
the actual imputation samples the `k→D` leg cross-slot via interpolation):

- **Hard floor** (O and D disconnected even ignoring detour, k=1, same-slot connectivity):
  **31,773,404 cells = 0.2398 of all / 0.2818 of NaN** — unrecoverable by any k / hops; this is
  the raw OD-sparsity physical limit (cross-slot interpolation lowers the true floor somewhat).
- **k-gate cost** ≈ remaining `0.49 − 0.24 = 0.25` of all cells — connectable but blocked by the
  k=3 source gate; recoverable by lowering k. (Confirmed by the k=1 run actually recovering
  `0.49 − 0.23 = 0.26` of cells.)
- Same-slot single-hop fillable fraction (of all cells, cumulative ≤3 hops): k=1 = 0.4696,
  k=2 = 0.2955, k=3 = 0.2095; average 22.6 corridor candidates per OD.

**Raw sparsity is OD-dimensional, not time-dimensional**: every slot has ≥48 observed OD
(median 1779, mean 1490, **0 empty slots**; night 3–5 h dips to ~400–650 but never empty),
whereas a typical OD pair is observed in only **408 / 13248 slots (3%)**; 949 OD (9.6%) are
**never** observed and 3888 (39.3%) appear in <1% of slots.

**Time-slot frequency** (count-merged from the 10-min matrix, no rerun): raw NaN
10 min 0.8510 → 20 min 0.7884 → 30 min 0.7471; share of observed cells with `count≥3`
0.260 → 0.354 → 0.408. Coarsening helps coverage modestly and mainly by raising counts
(relaxing the k≥3 gate), not by densifying raw coverage.

### 4.3 Parity with KoopmanTTE (read-only reference)

KoopmanTTE's own products (its `Processed/` parquets):

| Dataset | zones | OD | freq | rows | raw NaN | imputed NaN |
|---|---|---|---|---|---|---|
| this repo (fifth ring) | 100 clusters | 10000 | 10 min | 13248 | 0.8510 | 0.2315 (k=1) / 0.4913 (k=3) |
| KoopmanTTE Beijing | ~90 grid | 8100 | 10 min | 13248 | 0.8349 | 0.1240 |
| KoopmanTTE NYC (Manhattan) | ~67 grid | 4489 | 10 min | 17280 | 0.7821 | 0.2077 |

KoopmanTTE's preprocessing uses the same 10-min slots and 3–80 min trip filter, single-hop
transitive imputation, and **no source gate (effectively k=1)**. Since our raw baseline
(0.8510) and zone count (100 vs ~90) are close, the "~88% filled" of KoopmanTTE Beijing vs our
~51% at k=3 is **driven almost entirely by the k gate**, not by raw density, zone granularity,
slot frequency, or trip filter. Moving to k=1 closes most of the gap (0.4913 → 0.2315); the
residual (0.2315 vs 0.1240) is consistent with more/sparser OD (100 vs 90 zones) and the
stricter network-distance corridor versus Haversine.

### 4.4 Decision and known cost

`source_min_count` is set to **k=1** to maximize coverage and align with the KoopmanTTE
baseline. Known cost: held-out bound violation rises to 0.229 and `TTE_support` median drops to
1 (most fills rest on a single observation). This weakness is **captured by provenance**:
`TTE_support` flags the thin/weak fills so a downstream confidence-weighted hinge can down-weight
them. Coverage is bought back upstream; quality control is deferred to (not-yet-implemented)
downstream weighting.

## 5. Current status and deferred work

**Done:** baseline migration to clusters; network-distance topological pruning; bounded
multi-hop TDSP; edge-level source gate; provenance (`TTE_hops`, `TTE_support`); Stage 4 wired
into the package pipeline runner and covered by the repository test suite.

**Deferred (explicitly out of scope here):** the downstream KoopmanTTE confidence-weighted hinge /
MAE split consuming `TTE_support`; cluster-internal traversal cost (node-splitting `k_in→k_out`);
free-flow-time edge weights (maxspeed coverage insufficient); one-way directed routing;
train/val/test splitting; visualization.

## 6. Reproduction

```bash
conda run -n dydl pip install -e . --no-deps
# Full pipeline (partition → demand → supply → TTE):
conda run -n dydl roadnet-partition run --config configs/pipelines/full.yaml
# Stage 4 alone (uses the standalone fallbacks in the TTE config):
conda run -n dydl roadnet-partition tte --config configs/pipelines/tte.yaml
# Tests:
conda run -n dydl python -m pytest
```

Key knobs (`configs/pipelines/tte.yaml`, `stage4_tte.imputation`): `source_min_count` (k), `max_hops`,
`detour_ratio`, `use_validation`, `speed_limit_kmh`. The network distance matrix is built and
cached on first run (`stage4_tte.distance.recompute: false` reuses it). Products land in
the command-owned run directory; only the publish transaction writes
`data/processed/fifth_ring/tte/`.
