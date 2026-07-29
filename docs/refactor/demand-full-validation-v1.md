# Demand full validation v1

Phase 5A reran the mechanically migrated Demand implementation against the
current formal inputs. The isolated output is intentionally untracked and is
located at:

```text
outputs/refactor-validation/phase5a-demand/phase5a-full-v1/demand/
```

No file under `data/processed/` was modified.

## Baseline identities and allowed use

Two Demand baselines must remain distinct during the refactor.

### Historical formal baseline

`data/processed/fifth_ring/order_pipeline/` contains the Windows-generated
formal Demand products used by the current paper and historical downstream
results. This baseline is the input for historical Supply/TTE comparisons and
may be cross-platform revalidated if the historical environment becomes
available. It must not be moved, overwritten, regenerated, or silently replaced
during the refactor.

### Linux refactor baseline

`outputs/refactor-validation/phase5a-demand/phase5a-full-v1/demand/` is the
Linux same-environment baseline for validating the mechanically migrated Demand
implementation and for future Linux-only regression checks. It is ignored,
validation-only, and must not be published to `data/processed/` or described as
a row-for-row reproduction of the historical Windows products.

## Run result

- input rows: 90,105,866
- rows in the configured time window: 90,036,670
- assigned rows: 46,002,707
- exclusive rows: 30,651,791
- carpool rows: 15,350,916
- clusters: 100
- 10-minute slots: 13,248
- minimum departure: `2017-06-01 00:00:00`
- maximum departure: `2017-08-31 23:56:49`
- staging SQLite removed after successful OD/tensor generation: yes

The new Demand contracts passed for the cluster index, assigned orders, OD
CSV, tensor, metadata, and all three graph families.

## Phase 0 comparison

The following properties match the Phase 0 baseline exactly:

- cluster ID to index mapping;
- assigned-order row count and stage ID range;
- order IDs, driver IDs, timestamps, slots, and service labels row by row;
- service totals;
- tensor axes, shape, dtype, and sums;
- road graph edge set and both adjacency matrices;
- distance graph edge set (matrix differences are at most
  `2.7755575615628914e-17`);
- metadata statistics, apart from output paths.

The historical formal output is not position-for-position reproducible in the
current Linux environment:

- pickup segment differs on 6,434,254 assigned rows;
- dropoff segment differs on 8,102,197 assigned rows;
- origin cluster differs on 124,399 assigned rows;
- destination cluster differs on 127,633 assigned rows;
- `Y_total` differs in 457,784 cells, with maximum absolute difference 9 and
  unchanged total sum 46,002,707;
- the historical POI graph has 718 edges and the rerun has 719 edges (17
  historical-only and 18 rerun-only pairs).

The Phase 5A.1 spatial diagnostic supports environment-dependent
nearest-neighbour tie behaviour rather than migration logic:

- all non-spatial assigned-order identity, time, and service fields match;
- matched-row totals and POI matched-row totals are unchanged;
- most changed segment choices remain in the same cluster;
- the historical output stores Windows-style source paths while the rerun
  stores Linux-style paths;
- the migrated function bodies and SQL were already verified mechanically
  against the legacy implementation, and the old/new tiny end-to-end test is
  exact in one environment.

The read-only diagnostic compared point-to-candidate distances for every
origin/destination cluster difference. It used an absolute tolerance of
`1e-6 m` plus relative tolerance `1e-12` and produced only an ignored,
anonymous aggregate JSON:

```text
outputs/refactor-validation/phase5a-demand/phase5a-spatial-diagnostics-v1.json
```

Aggregate results:

- origin differences: 124,399 of 46,002,707 (`0.270417%`);
- destination differences: 127,633 of 46,002,707 (`0.277447%`);
- distinct affected orders: 251,455 (`0.546609%`);
- diagnostic origin/destination records: 252,032;
- exactly equal old/new candidate distance: 243,408 (`96.578%`);
- within tolerance but not bitwise equal: 8,624 (`3.422%`);
- old candidate clearly closer: 0;
- new candidate clearly closer: 0;
- overlapping old/new segment geometries: 252,032;
- multiple segments within the nearest-distance tolerance: 252,032;
- nearest projection at a candidate endpoint: 48;
- maximum candidate-distance difference: `5.684341886080802e-14 m`.

For 252,031 records there are exactly two candidates within the declared
nearest tolerance; one record has between five and nine. The submitted record
contains no coordinates, order/driver IDs, source rows, or segment IDs. These
results support overlapping-segment nearest-neighbour tie selection as the
observed cross-platform mechanism. No clearly non-equidistant anomaly was
found. Historical Python, GeoPandas, Shapely, GEOS and pyproj versions were not
recorded, so the exact historical dependency-level cause remains a Phase 9
gate.

No algorithm, spatial predicate, tie-break, ordering, dependency, or formal
output was changed to force agreement with the historical environment. Exact
cross-environment OD nonzero-position and POI-edge equality blocks Demand
re-publication and claims of historical row-level reproduction, but does not
block mechanical Supply/TTE migration.

## Phase 5B input isolation

The primary Phase 5B comparison must run both the legacy and migrated Supply
implementations against the same frozen historical input under
`data/processed/fifth_ring/order_pipeline/`, writing to separate validation
directories. A secondary Linux-only comparison may run both Supply
implementations against the Linux refactor baseline. Comparing legacy Supply on
historical Demand with migrated Supply on Linux Demand is invalid because it
mixes Demand platform differences into the Supply migration result.
