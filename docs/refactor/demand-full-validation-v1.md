# Demand full validation v1

Phase 5A reran the mechanically migrated Demand implementation against the
current formal inputs. The isolated output is intentionally untracked and is
located at:

```text
outputs/refactor-validation/phase5a-demand/phase5a-full-v1/demand/
```

No file under `data/processed/` was modified.

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

The evidence is consistent with environment-dependent nearest-neighbour and
floating-point tie behaviour rather than migration logic:

- all non-spatial assigned-order identity, time, and service fields match;
- matched-row totals and POI matched-row totals are unchanged;
- most changed segment choices remain in the same cluster;
- the historical output stores Windows-style source paths while the rerun
  stores Linux-style paths;
- the migrated function bodies and SQL were already verified mechanically
  against the legacy implementation, and the old/new tiny end-to-end test is
  exact in one environment.

No algorithm, spatial predicate, tie-break, ordering, dependency, or formal
output was changed to force agreement with the historical environment. Exact
cross-environment OD nonzero-position and POI-edge equality remains a blocker
for declaring the Phase 0 formal artifacts production-equivalent. It is not a
code-migration equivalence failure within one environment.
