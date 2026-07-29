# Demand migration inventory v1

This is the Phase 5A pre-migration code audit for `src/lib/order_dataset.py`
and `src/stages/stage2_demand.py`. It records current behavior; it does not
change the Demand contract.

## Definitions and ownership

`order_dataset.py` defines the constants `PROJECT_ROOT`, `EXCLUSIVE`,
`CARPOOL`, and ordered `SERVICE_TYPES`, plus 32 functions:

- project/config/output helpers: `project_path`, `display_path`,
  `load_project_config`, `active_scope_name`, `resolve_output_root`,
  `default_relation_edges_path`;
- ordering/time/service helpers: `sort_cluster_ids`,
  `floor_datetimes_to_slot`, `to_datetime_ns`, `to_epoch_ns`,
  `service_label_batches`, `infer_service_labels`;
- graph helpers: `normalize_adjacency`, `edges_to_adjacency`,
  `save_graph_assets`, `build_cluster_road_edges`,
  `empty_cluster_edge_frame`, `build_similarity_topk_edges`,
  `build_cluster_poi_graph`, `build_cluster_distance_graph`;
- OD helpers: `aggregate_od_frame`, `build_slot_labels`,
  `build_slot_labels_from_bounds`, `build_od_tensors`;
- partition/spatial helpers: `load_partition`, `build_cluster_index`,
  `match_points_to_segments_with_distance`;
- SQLite pipeline: `create_staging_database`, `stage_order_assignments`,
  `label_staged_service_types`, `export_assigned_orders`,
  `build_cluster_od_from_staging`, `load_staged_slot_bounds`;
- output/orchestration: `format_timestamp_ns`, `json_safe`, `main`.

`stage2_demand.py` accepts zero or one positional config path. With no argument
it uses repository `config.yaml`; it passes the path to `order_dataset.main`,
returns normally on success, and lets existing exceptions propagate. It has no
custom flags or exit-code translation.

## Inputs and path behavior

The pipeline reads `order_pipeline.inputs.partition_gpkg`, optional
`road_relation_edges_csv` (otherwise the active-scope processed edge file), all
`order_datasets`, and optional `poi_path`. Relative paths are currently resolved
against the repository root. The partition is loaded by GeoPandas, required to
contain `seg_id`, `cluster_id`, and a CRS, projected to `crs.projected`, and
converted to string identifiers. Orders use `pandas.read_csv` with the exact
configured `usecols`, configured `chunksize`, and pandas string dtype for order
and driver IDs. Datetimes are timezone-naive `datetime64[ns]` values.

The current production input is a 12,025,899,151-byte CSV with 90,105,866 rows.
The Phase 0 window retains 90,036,670 rows and stages 46,002,707 both-matched
orders. Phase 5A validation output must remain under
`outputs/refactor-validation/phase5a-demand/`.

## Filtering, matching and service labels

Valid times require parseable departure/finish values and `finish > departure`.
The configured window is half-open: `departure >= start_time` and
`departure < end_time`. Drivers must be non-null and non-blank. All four
coordinates must be finite. Pickup matching runs before dropoff matching, using
`geopandas.sjoin_nearest`, the configured geographic CRS, the partition CRS,
`max_distance_m`, and `drop_duplicates("row_id")`. Only orders with both matches
are staged; unmatched pickup/dropoff counts remain metadata statistics but are
not exported.

Service classification sorts by driver, departure, finish, and `stage_id`.
Intervals are half-open `[start, finish)`: touching intervals do not overlap.
Every connected overlap component is carpool if any overlap occurs; otherwise
it is exclusive. Batch emission defaults to 10,000 labels.

## SQLite lifecycle

The database is `<output>/orders_region_staging.sqlite`. An existing file is
unlinked before `sqlite3.connect`. `staged_orders` is created first with an
autoincrement `stage_id` and the existing column types/constraints. Chunks append
through `DataFrame.to_sql`. Afterwards the indexes
`idx_staged_driver_time(driver_id, departure_time_ns, finish_time_ns)` and
`idx_staged_slot_od(slot_start_ns, origin_cluster_id,
destination_cluster_id)` are created in that order. `service_labels` is dropped
and recreated, populated with `executemany`, then indexed by
`idx_service_type`. Commits and SQL statement order are migration invariants.
The connection closes in `finally`; the database is removed only after the OD
tensor was successfully built and `keep_staging_db` is false. Graph failures
occur after database cleanup, matching current behavior.

## Cluster and time ordering

Cluster IDs are deduplicated as strings. Integer-like IDs sort numerically
before non-numeric IDs, which sort lexicographically. `cluster_index` is the
enumeration of that order. Slot starts use pandas floor at the configured minute
frequency. Production uses 10-minute slots. The full tensor slot axis is the
continuous half-open range from minimum staged slot through one interval after
the maximum staged slot; Phase 0 contains 13,248 slots from
`2017-06-01 00:00:00` through `2017-08-31 23:50:00`.

## OD direction and tensor contract

Origin is pickup cluster and destination is dropoff cluster. SQLite aggregation
groups by slot, origin, destination, and service type, then pivots to
`exclusive_count`, `carpool_count`, and `total_count`. Rows sort by slot,
origin, destination. Tensor axes are `(slot, origin_cluster_index,
destination_cluster_index)`. `Y_exclusive`, `Y_carpool`, and `Y_total` are dense
`int32`; `slot_start` and `cluster_ids` are string arrays in a compressed NPZ.
Unknown slots or clusters are skipped by the current tensor builder.

The configured run writes exactly one suffix (`10min` in production or `15min`
in the tiny fixture). The simultaneous formal `cluster_od_10min.csv`,
`cluster_od_15min.csv`, `od_tensor_10min.npz`, and `od_tensor_15min.npz` files are
products of separate historical runs, not one invocation.

## Graph construction

All adjacency matrices use the complete ordered `cluster_index` count as shape,
not edge endpoints. Road edges map segment endpoints to clusters, discard
intra-cluster edges, canonicalize indices, sum non-negative configured weights,
and count contributing segment edges. POI rows are spatially matched, grouped by
cluster/category, written to `cluster_poi_features.csv` and
`cluster_poi_category_mapping.csv`, cosine-normalized, then converted to
symmetric top-k edges. Distance edges use projected centroids, Euclidean
distance, top-k neighbors, and `exp(-distance/decay)`.

`edges_to_adjacency` mirrors every edge into a float CSR matrix. Normalization
optionally adds an identity matrix; symmetric mode is
`D^(-1/2) A D^(-1/2)`, otherwise `D^(-1) A`. Raw and normalized matrices retain
isolated cluster rows. Phase 0 edge tables cover road=91, POI=99 and distance=100
cluster IDs, while every adjacency is 100x100.

## Outputs and schemas

One configured run writes:

- `cluster_index.csv`: cluster index/id, segment count, length, projected and
  geographic centroids;
- `orders_region_assigned.csv.gz`: ordered by `stage_id`, with source identity,
  order/driver IDs, formatted departure/finish/slot, matched segment and cluster
  IDs, match distances and service type;
- `cluster_od_<slot>min.csv` and `od_tensor_<slot>min.npz`;
- `cluster_poi_features.csv` and `cluster_poi_category_mapping.csv` when POI is
  enabled;
- `cluster_graph_{road,poi,distance}_edges.csv` and corresponding raw and
  normalized sparse NPZ files;
- `metadata.json` with active scope, input/output paths, slot frequency,
  cluster/segment counts, order statistics, service counts, data bounds, tensor
  slots, POI statistics, graph summaries, logical outputs, and the unchanged
  `order_pipeline` config.

`orders_region_assigned_sample_200k.parquet` exists in the formal directory but
has no writer or reader in the current repository. It is therefore a historical
derived sample, not an output of `order_dataset.main`; Phase 5A must not invent a
new generation rule. No current Demand code writes Parquet.

Empty-input behavior preserves an empty OD frame, zero-length slot axis and
zero-filled tensors. Invalid/unmatched rows contribute only to statistics.
`source_file` plus `source_row` records source identity, while `stage_id` is the
stable export ordering and SQLite primary key for a particular run.

## Downstream readers

Supply reads `orders_region_assigned.csv.gz`. TTE reads that file plus
`cluster_index.csv`. Documentation also names those inputs. No Supply/TTE code
reads OD tensors or graph assets in the current repository. Phase 5A does not
modify those consumers.

## Side effects and configuration readers

There is no mutable module cache. Import initializes geospatial environment and
repository constants. All other filesystem/database effects occur inside called
functions. Every `order_pipeline.*`, relevant `study_area.*`, and `crs.*` reader
will retain its legacy evidence in `config-key-map-v1.json` and gain a new
authoritative reader after migration. Root `config.yaml` remains unchanged.
