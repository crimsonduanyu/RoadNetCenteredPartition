# Supply migration inventory v1

This is the Phase 5B pre-migration audit of `src/lib/supply.py` and
`src/stages/stage3_supply.py`. It records current behavior without changing the
Supply algorithm or output contract.

## Definitions and serialization boundary

`src/lib/supply.py` defines 15 module constants and 32 functions. It defines no
class, dataclass, named tuple, enum, or other custom serializable type. A scan of
the repository found no pickle/joblib/cloudpickle/dill reference to
`lib.supply`; historical analysis scripts load the module dynamically from its
source path only. The compatibility bridge therefore needs to preserve public
constants and functions, but there is no custom pickle module path to retain.

The functions fall into these responsibilities:

- input/logging: `load_orders`, `configure_file_logging`;
- validation and trip construction: `filter_valid_orders`,
  `resolve_carpool_trip_groups`, `build_exclusive_trip_segments`,
  `build_trip_segments`;
- chains and idle state: `reconstruct_driver_chains`, `extract_idle_windows`;
- slots, clusters and interval expansion: `generate_slots`,
  `build_cluster_universe`, `complete_slot_cluster_grid`,
  `attach_global_fleet_to_all_clusters`, `_expand_interval_slots`;
- Supply variables: `compute_available_by_cluster`, `compute_in_service_od`,
  `compute_fleet_lower_bound`, `compute_supply_variables`;
- persistence/orchestration: `serialize_list_columns`, `save_csv_gz`,
  `process_orders_frame`, `write_json`, `merge_supply_with_demand`,
  `run_pipeline`;
- driver-block execution: `driver_block_id`, `build_global_slot_index`,
  `build_global_cluster_index`, `_slot_positions`, `_cluster_positions`,
  `run_chunked_pipeline`, `_inservice_array_to_frame`,
  `_dense_cluster_array_to_frame`, `_fleet_arrays_to_frame`.

Stage 3 defines `load_unified_config`, `build_parser`, and `main`. It accepts
`--orders-path`, `--output-dir`, `--max-gap`, `--tau-idle`,
`--carpool-merge-gap-s`, `--slot-duration`, and `--n-blocks`; exceptions are not
translated and success returns the summary dictionary.

## Inputs

The formal input is
`data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz`.
Supply reads only:

```text
order_id                 int64
driver_id                int64
departure_time           string -> timezone-naive datetime64[ns]
finish_time              string -> timezone-naive datetime64[ns]
origin_cluster_id        int32
destination_cluster_id   int32
service_type             category
```

Datetime parsing uses the exact `%Y-%m-%d %H:%M:%S` format with invalid values
coerced to `NaT`. Missing/non-positive intervals are logged and removed.
Integer dtype loading means missing or non-integer order, driver, or cluster IDs
fail during CSV parsing rather than being repaired. Supply does not read
`cluster_index.csv`, a Partition file, geometry, OD tensors, or graph assets.
The observed cluster universe is built from order origin and destination IDs.

Both `exclusive` and `carpool` are consumed. An unknown non-null service label
survives time validation but creates no trip segment because it is selected by
neither builder. There is no separate configured date filter: the full time
range in the input is processed.

`stage3_supply` defaults are:

```text
max_gap_minutes: 60
tau_idle_minutes: 30
carpool_merge_gap_s: 0
slot_duration_min: 10
n_blocks: 8
```

`demand_path`, `demand_dir`, and peak-hour keys exist in the unified config but
are not read by the formal Stage 3 path. Optional `merge_supply_with_demand`
uses `DEMAND_DIR/demand_table.csv.gz`, but Stage 3 never enables it.

## Trip, chain and idle semantics

Exclusive orders each become one segment. Carpool orders sort stably by driver,
departure, finish, and order ID. For the default merge gap zero, intervals with
strict overlap join one connected running-end group; touching intervals
(`gap == 0`) start a new group. A merged group takes the earliest-start order's
origin, latest-finish order's destination, and preserves grouped order IDs.

Unified trip segments sort stably by driver, start, end, and segment ID. Driver
chains split only when the next trip starts more than `max_gap_minutes` after
the previous trip ends. A gap exactly equal to the threshold remains in the
same chain. There is no midnight split: a short cross-midnight gap remains one
chain, while a long gap splits normally. Negative/overlapping gaps do not split
the chain.

Idle windows are consecutive trip gaps inside a chain. They use
`idle_start = previous trip_end`, `idle_end = next trip_start`, and the previous
trip's destination cluster. Only strictly positive gaps no longer than
`tau_idle_minutes` are retained. A negative gap raises `AssertionError` before
filtering. `tau_idle_minutes` is independent of chain formation.

Intervals overlap half-open slots. The last included slot is computed from
`end - 1ns`, so an interval ending exactly on a slot boundary does not occupy
the following slot. Cross-midnight intervals are not clipped by natural day.

## Supply variables

`supply_inservice_od` counts distinct drivers for each
`(slot_start, origin_cluster_id, destination_cluster_id)` occupied by a trip.
Origin is the trip pickup cluster and destination is the trip dropoff cluster.

`supply_available_floor` counts distinct idle drivers at their last-known
destination cluster for every slot overlapped by a retained idle window. The
formal chunked output is densified over every global `(slot, cluster)` pair;
zero rows are retained.

`supply_fleet_lower_bound` unions idle activity at the idle cluster with
in-service activity at the trip origin cluster only. It deduplicates a driver
within `(slot, cluster)` and within each global slot. The per-cluster and global
counts are then densified over the same global slot/cluster grid.

The global slot index is the continuous range from floor(min departure) to
ceil(max finish), exclusive of the upper boundary. The global cluster index is
the sorted union of observed order origins and destinations.

## Driver blocks and failure behavior

Drivers are assigned by the single expression
`pandas.util.hash_array(driver_ids) % n_blocks`. Every order for one driver
therefore belongs to one block. Blocks execute sequentially from zero through
`n_blocks - 1`; empty blocks are valid. Runtime assertions enforce disjoint
driver sets, complete driver coverage, valid global slot/cluster positions, and
int32-safe count ranges. Dense int64 arrays accumulate block results before CSV
conversion.

Block order cannot change the integer sum when each driver remains in exactly
one block. A different positive `n_blocks` changes allocation only, not intended
semantics. The current implementation does not explicitly validate
`n_blocks > 0`; zero fails later because no drivers are covered.

The formal path writes directly into its output directory. It has no temporary
database, worker process, daily part, resume marker, `_SUCCESS`, or current
partial-summary writer. `config_used.json` and `run.log` can remain after a
failure, and a failure during output writing can leave an incomplete file.
`run_summary.json` is written only after all three formal CSV files succeed.
The historical `run_summary.partial.json` is not part of current recovery
behavior.

## Formal outputs

The current writer produces:

- `supply_inservice_od.csv.gz`: columns `slot_start`,
  `origin_cluster_id`, `destination_cluster_id`, `vehicles_in_service`; sparse
  nonzero OD rows sorted by slot/origin/destination; primary key is those three
  key columns;
- `supply_available_floor.csv.gz`: columns `slot_start`, `cluster_id`,
  `available_vehicles`; dense sorted slot/cluster grid; primary key is
  `(slot_start, cluster_id)`;
- `supply_fleet_lower_bound.csv.gz`: columns `slot_start`, `cluster_id`,
  `fleet_lower_bound_cluster`, `global_fleet_lower_bound`; dense sorted grid;
  primary key is `(slot_start, cluster_id)`;
- `run_summary.json`: execution mode, thresholds, blocks, input/driver/grid
  counts, formal row counts, note, and per-block summaries;
- `config_used.json`: actual run arguments, including input/output paths.

`run.log` is diagnostic, not a formal data product. When demand merging is
explicitly enabled outside Stage 3, `supply_demand_merged.csv.gz` is an optional
extra, not part of the current formal allowlist.

## Historical files and repository readers

Current code does not generate `supply_in_service_od.csv.gz`,
`supply_available_by_cluster.csv.gz`, `trip_segments.csv.gz`,
`driver_chains.csv.gz`, `idle_windows.csv.gz`, or
`run_summary.partial.json`. They remain historical products in the formal
directory. Old analysis scripts and `tmp.ipynb` reference them; current formal
tests read only the new filenames. No current Supply/TTE production reader was
found for either formal Supply table set. Repository-external consumers remain
unknown and require manual confirmation.

## Historical analysis conclusions retained by tests

Existing tests already cover carpool overlap grouping, stable trip ordering,
the inclusive 60-minute chain boundary, removal of the midnight chain break,
half-open slot expansion, cross-midnight in-service slots, separation of
`tau_idle` from chain formation, origin-only fleet attribution, driver
deduplication, and a chunked-versus-whole-frame comparison.

Phase 5B will retain the historical scripts unchanged. It will strengthen the
tests for block coverage/disjointness, block count/order independence, empty
blocks, formal output contracts, and partial-failure non-success behavior.

## Privacy and serialization conclusion

Full Supply validation outputs contain driver-derived aggregates and remain
ignored. No driver/order IDs, coordinates, trip samples, chain records, or idle
windows may be committed. No custom Supply serialization type or historical
pickle module path was found.
