# Production config equivalence v1

## Result

The six committed split files parse read-only and preserve the current effective production values. The audit checked all 341 source mapping keys, compared 151 authoritative split-routed value keys, and found 151 effective-value matches. All 36 authoritative path comparisons resolve to the same absolute targets; all 41 path-valued source keys remain classified in the audit, including root-only legacy/experiment paths. Repeated comparisons for `crs`/center-point values in both dataset files do not create a competing stage value.

## Split files

- `configs/datasets/fifth_ring.yaml`
- `configs/datasets/fourth_ring.yaml`
- `configs/zoning/regularized.yaml`
- `configs/pipelines/demand.yaml`
- `configs/pipelines/supply.yaml`
- `configs/pipelines/tte.yaml`

Each stage file uses `dataset_config: ../datasets/fifth_ring.yaml` and `scope: fifth_ring`. Dataset `project_root: ../..` and every other relative path are interpreted from the file containing the value, not from the process current working directory.

## Identical parameters

- Demand preserves the complete `order_pipeline` mapping: input fallbacks, SQLite/chunk size, half-open order time window, 10-minute slots, service columns, POI matching, graph weights, distance graph, and normalization.
- Supply preserves the authoritative `stage3_supply` subset: `orders_path`, `output_dir`, `max_gap_minutes=60`, `tau_idle_minutes=30`, `carpool_merge_gap_s=0`, `slot_duration_min=10`, and `n_blocks=8`.
- TTE preserves the complete root `stage4_tte` mapping: filenames, network-distance construction, inclusive time axis, trip-time band, keep-place gates, and imputation/validation values.
- Partition preserves the canonical Regularized inputs, initialization, objective, search, outputs, and all production numerical values. Input and expected-partition paths resolve to the same frozen files.

## Intentional structural changes

- `study_area.scopes.<scope>` becomes one dataset file per scope: top-level `scope`, common `crs`, `study_area` metadata, and `paths` assets.
- `schema_version: 1`, `project_root`, dataset raw/interim/processed roots, and the stage-level `dataset_config`/`scope` fields are new explicit structure; they do not replace an old numerical parameter.
- Partition adds `contract.verify_canonical: true`; the resolver reuses `stage1_partition.outputs.canonical_partition` as the expected file instead of duplicating its path.
- TTE adds optional `stage4_tte.inputs.network_distance_path` and `representative_nodes_path` standalone fallbacks. Root `config.yaml` has no corresponding keys.
- Public run ownership replaces each configured standalone output directory at execution time. The configured paths remain only fallbacks/equivalence evidence.

## Partition effective-value note

Root `config.yaml` contains `stage1_partition.regularized.search.grid.merge_split_enabled: [false, true]`, but the legacy canonical Stage 1 adapter always replaces it with `[false]` and also forces `allow_merge_split: false`. The split production config records that effective canonical value directly. This is the sole raw-text value difference and is not an algorithm or search-parameter change.

## Path changes

Path strings gained `../..` prefixes because their base moved from repository root to `configs/zoning` or `configs/pipelines`; normalized absolute paths are unchanged. Dataset paths are independently based at `configs/datasets`. Filename-only fields such as `TTE_count.parquet` remain basenames and must not be resolved as input paths or allowed to escape a stage directory.

The Phase 1 map marked 34 path leaves. This audit treats 41 leaves as path-valued and adds the seven previously unclassified paths listed in `production-config-split-v1.md`.

## Legacy and retained-only keys

The 97 top-level legacy preprocessing/baseline/reporting keys remain only in root `config.yaml`; they are not forced into dataset or production stage config. Partition `stage1_partition.baseline`, `regularized.evaluation`, and `regularized.visualization`, plus Supply `demand_path`, `demand_dir`, and peak-hour fields, also remain root-only because the authoritative runners do not consume them. All 156 root-only mapping/value entries and the 30 no-reader entries remain explicit in the audit.

## CLI resource overrides

The existing formal resource key is Supply `stage3_supply.n_blocks`, exposed as `--n-blocks`. Its effective override must be written into the resolved config, config fingerprint, and manifest. Current Stage 3 has no `workers` or `chunk_size` config key, so Phase 6A must not invent aliases or silently accept arbitrary dotted-key overrides.

## Standalone fallbacks

Demand `outputs.root`, Supply `output_dir`, TTE `output_dir`, and Partition `outputs.run_root` preserve old standalone destinations as configuration evidence. A public single-stage CLI writes only to its owned run stage directory. Demand's canonical Partition input, Supply's assigned-orders input, and TTE's assigned-orders/cluster/distance inputs remain explicit stage-local fallbacks rather than implicit dataset overrides.

## Demand platform gate

The fifth-ring Demand config is runnable on Linux, but a Linux rerun is not a historical Windows order-by-order reproduction because spatial equidistance can select a different segment. Phase 6A performs tiny CLI validation only, does not publish Demand, and does not add a deterministic assignment tie-break. The full platform baseline remains Phase 9 work.

## Deferred dependencies

`python-louvain`, `python-igraph`/`leidenalg`, and `pymetis` remain deferred and are not installed by Phase 6A. Partition tiny CLI must use the canonical Regularized path or a fixed fixture that does not require those baseline packages.

## Network-distance helper debt

`roadnet_partition.graphs.distance.project_path`, `load_project_config`, and `sort_cluster_ids` remain in place. Split-config path equivalence does not authorize changing their standalone behavior. Consolidation stays deferred unless it is isolated, preserves `lib.network_distance` public names, and proves path-equivalent behavior.

## Uncertain items

The 30 keys without confirmed static reader evidence and three entries with truncated reader evidence remain explicitly recorded in the split audit. Root-only values remain preserved in `config.yaml`, not silently discarded. Root `config.yaml` remains authoritative for old wrappers throughout Phase 6A.
