# Raw-only clean-room run vs Linux canonical — parity audit v1

- Date: 2026-07-31
- Clean-room run: `outputs/runs/raw-only-reproduction`
  - clean-room: `/home/dy/jupyter/workspace/RoadNetCenteredPartition-cleanroom`
  - environment: fresh `conda env create --prefix ./.conda/dydl -f environment.yml`
  - runtime: Python 3.11.15, pandas 3.0.5, geopandas 1.1.4, numpy 2.4.6, scipy 1.17.1, pyarrow 25.0.0, shapely 2.1.2
- Linux canonical: `data/processed/fifth_ring/` (published tree)
  - source run: `20260730T020500Z-fifth-ring-full-02fce2f7`
  - source commit: `e54077b746607001e6fdf45ed09530a03c851179`
  - runtime: Python 3.12.13, pandas 3.0.3, geopandas 1.1.3, numpy 2.4.6, scipy 1.18.0, pyarrow 24.0.0, shapely 2.1.2
  - published: 2026-07-30T05:15:37Z, validation status `passed`

Method: strict SHA-256 on all 30 published files; for differing files,
decompressed row/aggregate comparison (gzip), array comparison (npz/parquet),
and semantic grouping comparison (partition). Provenance/path fields are
compared separately from data values.

## 1. Byte-level summary

| Result | Count | Files |
| --- | --- | --- |
| SHA-256 equal | 1 | `order_pipeline/cluster_poi_category_mapping.csv` |
| SHA-256 differ | 29 | all other published files |
| Missing in run | 0 | — |

## 2. Partition grouping

- Segment count identical: 59,096; cluster count identical: 100.
- Columns: canonical `canonical_partition.csv` has a `bearing` column that the
  run output does not (30 vs 29 columns); all other columns identical.
- Cluster assignment on the 59,096 shared `(u,v,key)` segments:
  - exact label match: 0.04%
  - best-aligned label match (greedy 100x100 contingency): 57.4%
  - Adjusted Rand Index: 0.663
- Verdict: segment set and cluster count match, but grouping is **not**
  equivalent.

## 3. Preparation stage (root cause)

Canonical Phase 9 consumed frozen inputs
(`data/interim/fifth_ring/frozen_inputs/*`, provenance source
`the legacy intermediate-data provenance tree/...` and old `data/processed/fifth_ring`);
the clean-room run consumed run-owned preparation regenerated from
`data/raw/` by the new `src/roadnet_partition/pipeline/preparation.py`.

| Asset | Frozen | Run-owned | Row/vertex comparison |
| --- | --- | --- | --- |
| `segment_nodes.gpkg` | 16,502,784 B | 15,933,440 B | rows equal (59,096), seg_id overlap 59,096/59,096 |
| `segment_order_features.csv` | 25,329,259 sum | 25,653,444 sum | rows equal (59,096), numeric totals **differ** |
| `segment_poi_features.csv` | 101,728,046.21224871 | 101,728,046.21223775 | rows equal, float-level difference |
| `segment_relation_edges...csv` | 14,917,822.9 sum | 14,918,568.8 sum | rows equal (228,243), edge weights **differ** |
| `segment_order_od_hourly.csv` | 2,651,430 rows, total 2,683,690 | 2,651,488 rows, total 2,683,690 | rows **differ** (+58), totals equal |
| relation graph (gpickle) | 59,096 nodes / 228,243 edges | 59,096 / 228,243 | topology counts equal |

The same order/POI/road raw inputs produce different preparation values under
the new preparation code and runtime environment; totals are preserved but
segment-level values and hourly OD layout differ.

## 4. Demand

- `orders_region_assigned.csv.gz`: decompressed row count identical
  (**46,002,707** both).
- `metadata.json`: key differences are provenance/paths only
  (output_root, outputs, partition_gpkg, road_relation_edges_csv,
  graph_summaries edge counts 261/719/583 vs 265/730/584).
- `cluster_index.csv`, `cluster_graph_{road,poi,distance}_edges.csv`,
  adjacency npz, `cluster_poi_features.csv`: differ (consistent with the
  different partition grouping; e.g. road edges 261 vs 265, POI edges 719 vs 730).
- `cluster_od_10min.csv`: 19,897,886 vs 19,507,610 rows (different sparsity).
- `od_tensor_10min.npz`: per-channel sums identical
  (Y_total 46,002,707; Y_exclusive 30,651,791; Y_carpool 15,350,916), arrays
  differ — consistent with cluster-label permutation of the same order totals.

## 5. Supply

| Table | Canonical rows | Run rows |
| --- | --- | --- |
| `supply_available_floor.csv.gz` | 1,324,800 | 1,324,800 |
| `supply_fleet_lower_bound.csv.gz` | 1,324,800 | 1,324,800 |
| `supply_inservice_od.csv.gz` | 41,890,744 | 41,147,628 |

`run_summary.json` differs only in `in_service_rows`; shared constants match
(orders_loaded 46,002,707, n_drivers 199,668, slots 13,248, clusters 100,
n_blocks 8, tau 30, max_gap 60).

## 6. TTE — observed / inferred / missing

| Metric | Canonical | Run | Consistent? |
| --- | ---: | ---: | --- |
| shape | 13248 x 10000 | 13248 x 10000 | yes |
| observed (non-NaN in [3,80]) | 19,730,545 | 19,343,240 | **no** |
| inferred (hops >= 1) | 82,080,529 | 84,241,693 | **no** |
| missing (hops < 0) | 30,668,926 | 28,895,067 | **no** |
| `TTE_count` total | 45,665,595 | 45,665,595 | yes |
| `cluster_network_distance.parquet` | — | — | index equal (0..99), values **differ** |
| `cluster_representative_nodes.csv` | — | — | **differ** |

Count totals are identical while the observed/inferred/missing split is not:
the same order counts redistribute across different cluster pairs because the
partition grouping differs. TTE also differs in how distance inputs were
produced: canonical used precomputed `network_distance_path` /
`representative_nodes_path` fallbacks, the raw-only run recomputes distance
from `data/raw` GraphML and the same-run Partition.

## 7. Root-cause attribution

1. **Preparation differs** (new run-owned preparation vs frozen inputs used by
   Phase 9): segment-level order features, relation weights, and hourly OD
   layout differ numerically while preserving global totals.
2. **Runtime environment differs**: Python 3.12.13/pandas 3.0.3/geopandas
   1.1.3/scipy 1.18.0/pyarrow 24.0.0 (canonical) vs Python 3.11.15/pandas
   3.0.5/geopandas 1.1.4/scipy 1.17.1/pyarrow 25.0.0 (run).
3. **Config/verification changes**: `configs/zoning/regularized.yaml` changed
   `contract.verify_canonical` and added `standalone.output_dir`;
   `configs/pipelines/tte.yaml` switched from precomputed distance fallbacks to
   raw recompute; pipeline/demand/supply configs otherwise semantically equal
   (path-normalized).

## 8. Gate conclusion

Per the release gate: **TTE observed/inferred/missing are NOT consistent with
the Linux canonical.** Therefore the raw-only clean-room run is not a
byte/semantic reproduction of the published canonical, and committing the
current 52-file change set is **stopped** pending a determinism/baseline
decision. Recommended next steps (not executed here):

- pin the environment to the canonical runtime versions and rerun to isolate
  version effects;
- verify run-owned preparation against the frozen inputs value-by-value and
  decide whether to re-baseline the canonical after the raw-only refactor;
- if re-baselining is approved, publish a new Linux canonical from a raw-only
  run and record the new baseline decision.
