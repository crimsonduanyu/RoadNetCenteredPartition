# TTE migration inventory v1

This is the read-only Phase 5C inventory of the current Stage 4 behavior. It
records the implementation and formal products before the authoritative code
moves from `src/lib/tte_dataset.py` to
`src/roadnet_partition/downstream/tte.py`. It does not change TTE mathematics,
configuration, paths, or data.

## Current entry points and ownership

The current call chain is:

```text
src/run_pipeline.py
  -> subprocess: src/stages/stage4_tte.py
  -> lib.tte_dataset.main
  -> lib.tte_dataset.run_from_config
  -> lib.network_distance.build_or_load
  -> roadnet_partition.graphs.distance.build_or_load
```

`stage4_tte.py` accepts the first positional argument as a config path and
otherwise uses the repository-root `config.yaml`. It has no argparse help or
additional flags. Exceptions propagate and produce a non-zero process exit.
Supply is neither an input nor a dependency.

Phase 5C maps the existing TTE constants, `SpatialPruner`, calculation helpers,
`run_from_config`, and `main` mechanically to
`roadnet_partition.downstream.tte`. `graphs/distance.py` remains authoritative
for representative-node selection and the static cluster network-distance
matrix. No repository pickle, joblib, cloudpickle, dill, dynamic import, or
serialized custom type references `lib.tte_dataset` or `SpatialPruner`.

## Inputs and cluster axis

The formal assigned-order reader loads the complete CSV once, restricted to:

```text
departure_time
finish_time
origin_cluster_id
destination_cluster_id
```

Order ID, driver ID, `service_type`, the stored `slot_start`, coordinates, and
segment IDs do not participate in TTE. The current historical input is:

```text
data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz
```

It contains 46,002,707 rows. Cluster membership is derived from this order
table before trip-duration filtering: an ID must meet both configured origin
and destination frequency thresholds. IDs use the existing numeric-first
`sort_cluster_ids` order. The full OD axis is origin-major then
destination-minor:

```text
0->0, 0->1, ..., 0->99, 1->0, ..., 99->99
```

The implementation does not require exactly 100 clusters. It does not derive
the TTE cluster axis from `cluster_index.csv`, and it does not currently verify
that the cached distance axis exactly equals the kept-order cluster axis.

## Network distance and representative nodes

With the formal `recompute: false` setting and an existing cache, Stage 4 reads
only:

```text
data/processed/fifth_ring/tte/cluster_network_distance.parquet
```

The matrix is float64 metres, 100 by 100, string-indexed in the same order on
both axes, finite, symmetric, and zero-diagonal. TTE divides it by 1000 before
constructing `SpatialPruner`, whose validation and speed calculations use
kilometres. The formal matrix range is 0 to 38,018.03963639683 metres.

`cluster_representative_nodes.csv` has 100 rows with:

```text
cluster_id, rep_osmid, dist_to_centroid_m
```

It is an output of distance construction, not a TTE runtime reader. On a cache
hit, `cluster_index.csv`, GraphML, classified edges, the canonical partition,
and representative-nodes CSV are not read or validated.

On a cache miss or `recompute: true`, `graphs/distance.py` owns the build:

- load the full OSM graph and collapse it to an undirected minimum-length graph;
- build the classified edge-induced subgraph, retaining raw edge lengths;
- select each cluster's node nearest its projected centroid, with ascending
  `osmid` providing the existing exact-tie order;
- use filtered-graph shortest paths, falling back to the full graph for
  unreachable filtered pairs;
- symmetrize the matrix and write the distance and representative-node files.

The distance matrix index order controls corridor enumeration and exact
imputation tie-breaking and must not be normalized during mechanical migration.

## Raw TTE and count

Trip duration is:

```text
(finish_time - departure_time).total_seconds() / 60
```

Rows are retained inclusively inside the configured `[min_minutes,
max_minutes]` band. Under the formal 3-to-80-minute band, missing, negative,
zero, and shorter durations are excluded by the same expression; no separate
duration rule exists. The slot belongs to the floored pickup/departure time.
Pickup time determines the OD observation slot; drop-off time only determines
duration.

The pipeline performs one groupby over `(slot, origin, destination)` and emits:

- `TTE_raw`: configured aggregation, currently median, float64;
- `TTE_count`: non-null supporting trip count, int32.

The grouped table is pivoted and reindexed to the complete configured time and
OD axes. Therefore `count > 0` is exactly the raw observed mask. Departures
outside the configured axis disappear during reindexing. The formal axis is
13,248 timezone-naive 10-minute slots from `2017-06-01 00:00:00` through
`2017-08-31 23:50:00`, and 10,000 OD columns.

Formal raw facts are:

```text
finite cells       19,742,327
missing cells     112,737,673
finite range             3..80 minutes
count total        45,665,595
diagonal observed     811,154
```

## Imputation, support, and hops

`TTE_imputed` begins as `TTE_raw.astype(float32)`. Observations are never
logically overwritten, but their stored imputed values equal the float32 cast,
not the float64 raw bit pattern.

For a target `O->D`, `SpatialPruner` enumerates intermediate `k` values in its
distance-matrix order when:

```text
d(O,k) + d(k,D) <= detour_ratio * d(O,D)
```

The endpoints are excluded. `d(O,D) < 0.01 km`, including every diagonal,
produces no candidates. For each candidate:

```text
T_OD(t) = T_Ok(t) + interpolate(T_kD, t + T_Ok(t))
```

Interpolation outside the time axis returns NaN. Candidate estimates use the
minimum value; an exact tie keeps the first candidate in matrix order. The
selected path support follows that same argmin. The second leg's support uses
the nearest arrival slot from `np.rint`, clipped to the time axis; it is not
interpolated.

Each imputation round reads a frozen prior-round value/support snapshot. An
observed cell can act as a source only when `count >= source_min_count`; thin
observations remain unchanged in the output but are masked as source legs.
Previously inferred values may become later-round sources. Validation then
applies, in the existing order:

1. target network distance exists and is at least `min_dist_km`;
2. implied speed stays inside the configured range;
3. the centered rolling median/std outlier rule accepts the estimate.

Only currently missing cells are filled. A round with no fills stops the loop.

`TTE_hops` is not a road-graph hop matrix. It is int16 imputation provenance:

```text
 0  raw observation
 r  filled in imputation round r
-1  still unfilled
```

`TTE_support` is not a boolean support or reachability mask. It is int32 and
stores the selected inferred path's weakest observation count only where
`hops >= 1`; raw observations and unfilled cells both store `-1`.

Formal distributions are:

```text
hops -1   30,666,386
hops  0   19,742,327
hops  1   79,604,455
hops  2    2,092,889
hops  3      373,943
newly inferred 82,071,287
final finite  101,813,614
```

Diagonal observations are allowed and are not assumed zero. Missing diagonal
cells are never imputed. The formal diagonal contains 811,154 finite raw and
imputed values and 513,646 missing values; support is `-1` throughout.

## Output contract and serialization

The stable case-sensitive filenames are:

```text
cluster_network_distance.parquet
cluster_representative_nodes.csv
TTE_raw.parquet
TTE_count.parquet
TTE_support.parquet
TTE_hops.parquet
TTE_imputed.parquet
```

The five TTE matrices share the exact index and OD axes. Their logical dtypes
are float64, int32, int32, int16, and float32 respectively. The datetime index
is unnamed, the columns axis is named `OD`, and pandas writes the index as the
Parquet index field. The current writer calls `DataFrame.to_parquet(path)` with
no explicit engine, compression, row-group, or version arguments; these writer
defaults are part of the behavior to preserve in Phase 5C.

The current five formal matrices are each one Parquet row group and total about
709 MiB with the two distance assets. `run_from_config` writes no summary,
metadata, config snapshot, `_SUCCESS`, or manifest; it returns an in-memory
summary and `main` prints it in insertion order. An interrupted legacy run may
leave a valid-looking prefix of files, so `run_tte` must only return COMPLETE
after every required file exists and passes its contract. It must not change
the underlying writer or invent a success marker in this phase.

## Configuration readers and defaults

The current reader is the `stage4_tte` section of the unified root config.
Important defaults are:

```text
trip_time.aggregation                 median
keep_place.min_origin_orders          1
keep_place.min_dest_orders            1
outputs.count_filename                TTE_count.parquet
outputs.hops_filename                 TTE_hops.parquet
outputs.support_filename              TTE_support.parquet
distance.matrix_filename              cluster_network_distance.parquet
distance.representatives_filename     cluster_representative_nodes.csv
distance.recompute                    false
imputation.max_hops                   3
imputation.source_min_count            3
imputation.detour_ratio               1.3
imputation.speed_limit_kmh             [5, 120]
imputation.min_dist_km                 0.01
imputation.window                      6
imputation.outlier_std_threshold       3
imputation.use_validation              true
```

The formal root config overrides `source_min_count` to 1. The configured
`imputation.method` key is currently not read or validated and must not acquire
new behavior during migration.

Legacy config paths and their relative data paths resolve against the repository
root. `ResolvedStageConfig` paths resolve against the stage config file. Phase
5C must preserve both interfaces rather than silently changing Stage 4 path
semantics.

## Frozen validation inputs and resources

The old/new production-scale comparison will use the same four frozen assets:

| Asset | SHA-256 |
|---|---|
| historical assigned orders | `f6dfbbf09dc0fb8cc8d14c00e929e54484303ef997599b5266420048eaea6969` |
| historical cluster index | `5b7bf80aca473aec87e42394d9a0a66581d00a044ecae4f8224eb2fbc7301826` |
| formal network distance | `69fe2a603efd637bc8c1207fd1cfab417aefa699231d25b04fd73c8e8ac6953a` |
| formal representative nodes | `1d550ba86a50dacf27ab41111ea9650bee6ad5d83fdc6fa91ceed89d15b9d6e1` |

Because the distance cache lives inside `output_dir`, both ignored validation
directories must be pre-seeded with the same distance and representative-node
files and use `recompute: false`; otherwise an empty output directory triggers
a rebuild and violates the common-input rule.

The current host has sufficient capacity for sequential full runs: about 245
GiB free disk and 53 GiB available memory at audit time. Two complete
validation outputs add about 1.4 GiB. Full old/new runs must remain sequential.
Large Parquet comparisons will use PyArrow record batches inside the single row
group, so old, new, and formal matrices are never all loaded at once.

## Migration invariants and deferred debt

Mechanical migration must preserve function bodies, signatures, defaults,
cluster/OD/time order, groupby order, float casts, candidate order, tie-breaks,
round snapshots, NaN propagation, diagonal behavior, file names, writer calls,
stdout statistics, exceptions, and cache behavior.

`project_path`, `load_project_config`, and `sort_cluster_ids` still live in
`graphs/distance.py` for standalone compatibility, while Demand contains a
same-behavior sort helper. Phase 5C may make TTE depend directly on the existing
distance helpers, but it will not modify Demand or force a new path contract.
Further consolidation remains explicit debt before Phase 6 if it cannot be done
without standalone behavior risk.
