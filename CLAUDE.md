# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project location

The active project is the repository root. Source code, configuration, data artifacts, and outputs live directly under this directory.

## Environment and commands

Set up the conda environment from the project root:

```bash
conda env create -f environment.yml
conda activate bj_road_partition
```

If `python-louvain` is unavailable through conda, install it with `pip install python-louvain`.

Run the full three-stage pipeline from the repository root:

```bash
python src/run_pipeline.py
```

Run an individual stage (each reads the unified `config.yaml`):

```bash
python src/stages/stage1_partition.py            # spatial partitioning (regularized search)
python src/stages/stage1_partition.py --verify   # reproduce canonical partition + check vs frozen copy
python src/stages/stage2_demand.py               # demand dataset (OD / POI / distance graphs)
python src/stages/stage3_supply.py               # supply-state reconstruction
```

Run the tests:

```bash
python -m pytest tests/
```

VSCode can run the currently open script with the Run button using the workspace settings in `.vscode/`, which select the `bj_road_partition` conda interpreter.

## Architecture overview

This is a Python geospatial pipeline for road-centered semantic partitioning of the drivable road network inside Beijing Fifth Ring Road, plus demand and supply dataset construction on top of that partition. The key road-modeling choice is to keep ordinary road segments as graph nodes while compressing short OSM `*_link` connector roads into relation edges between adjacent ordinary segments.

The codebase is organized as **pure library code** under `src/lib/` (importable, no I/O, no top-level side effects) and thin **run scripts** under `src/stages/` that read the single `config.yaml` and orchestrate I/O. Reproducing a result = run the stage script; changing parameters = edit `config.yaml`.

The pipeline has three sequential, file-based stages:

**Stage 1 — spatial partitioning** (`src/stages/stage1_partition.py`, core in `src/lib/regularized.py`).
The road relation graph and POI/order segment features are built upstream (see *Legacy baseline pipeline* below), and baseline clusterings (louvain / leiden / demand_region_growing) seed a **regularized local search** that optimizes a capacity-balanced, cut-minimizing objective to produce the canonical 100-cluster partition. The search is deterministic; it is initialized from the **leiden** baseline. Stage 1 reads its inputs and baselines from the frozen reproducibility snapshot (see *Reproducibility*), regenerates the canonical partition, and `--verify` checks it is grouping-equivalent to the frozen copy.

**Stage 2 — demand dataset** (`src/stages/stage2_demand.py`, core in `src/lib/order_dataset.py`).
Reads the frozen canonical partition and the raw order table, assigns order pickup/dropoff points to clusters through a SQLite staging database, infers exclusive/carpool service types from driver time overlaps, exports assigned orders, and builds the cluster OD table + OD tensor and the road / POI / distance cluster graphs (raw and normalized adjacency).

**Stage 3 — supply state** (`src/stages/stage3_supply.py`, core in `src/lib/supply.py`).
Reads Stage 2's `orders_region_assigned.csv.gz`, reconstructs per-driver trip chains and idle windows, and computes slot-level available-by-cluster, in-service-OD, and fleet-lower-bound supply tables. Two correctness fixes live here: **Fix-1** clips each interval's slot expansion to its natural day (eliminates cross-midnight slot bleed / duplicate rows), and **Fix-2** attributes in-service drivers to the origin cluster only.

`src/run_pipeline.py` runs the three stage scripts in order in isolated subprocesses (Stage 2 is memory-heavy), with Stage 1 in `--verify` mode so a reproduction mismatch fails the run.

## Library modules (`src/lib/`)

- `geo.py` — project paths, config/CRS loading, OSM value normalization, road-name matching, ring polygon construction, boundary validation, bounds projection, bearing/angle helpers, GeoPackage-safe conversion. (Owns `PROJECT_ROOT`, `project_path`.)
- `graph.py` — relation-graph edge bookkeeping: canonical segment pairs, incident-node indexing, edge-record creation/serialization.
- `metrics.py` — partition/benchmark metrics (edge cut, connectivity, shape, OD, road integrity, etc.). `EPS = 1e-9` here is a metric safe-divide floor.
- `regularized.py` — regularized-search objective + local search (move/merge/split) and `run_from_config`. `EPS = 1e-12` here is an objective tolerance (intentionally distinct from `metrics.EPS`).
- `order_dataset.py` — Stage 2 core (order staging, service-type inference, OD tensors, cluster graph assets).
- `supply.py` — Stage 3 core (driver chains, idle windows, supply tables) including Fix-1/Fix-2.

## Configuration

`config.yaml` is the single source of truth and drives all three stages. Shared sections: `study_area`, `crs`, `road_filter`, `connector_rules`, `continuity`, `graph_weights`, `semantic_graph`. Stage sections:

- `stage1_partition` — `graph_variant`, `baseline` (algorithms + per-algorithm resolutions), and `regularized` (`inputs`, `baseline_clusters`, `objective`, `search`, `evaluation`, `visualization`), plus `outputs.canonical_partition`.
- `order_pipeline` — Stage 2 inputs/outputs (its `inputs.partition_gpkg` points at the frozen canonical partition).
- `stage3_supply` — orders path, output dir, slot/gap parameters.

Prefer changing behavior through `config.yaml` when the needed parameter already exists. (The legacy `clustering`, `evaluation`, and `visualization` sections drive the legacy baseline scripts `src/03`–`src/05`.)

## Reproducibility

`IntermediateDataForReproduce/` is a frozen snapshot of everything needed to regenerate the canonical partition deterministically: the regularized-search inputs (graph, segment nodes/edges, POI/order features, hourly OD, classified edges, boundary), the **random** baseline cluster products (leiden/louvain/demand_region_growing — not bit-reproducible because of random seeding and the live OSM harvest), and the canonical regularized output. `config.yaml` points Stage 1's regularized inputs/baselines and Stage 2's partition input at this folder.

Rules: never overwrite files here. Stage 1 re-runs write to a `*_stage1_verify` sibling directory under `regularized_zoning_experiments/runs/` and are compared against the frozen canonical, never written into the snapshot. The binaries are git-ignored; only `IntermediateDataForReproduce/README.md` is tracked (it documents provenance).

## Legacy baseline pipeline and experiment code

These produce the (now frozen) upstream artifacts and are not part of the day-to-day three-stage run:

- `src/00_download_osm.py` → `src/01_preprocess_roads.py` → `src/02_build_*_features.py` / `src/02_build_segment_relation_graph.py` build the road graph and segment features. `src/03_cluster_segments.py` + `src/adaptive_clustering.py` produce the baseline clusterings; `src/04_visualize_clusters.py`, `src/05_benchmark_clusters.py` visualize/benchmark them. Stage 1 of `00`/`01` uses live OSMnx downloads.
- All code imports the shared utilities from `lib.geo` / `lib.graph` / `lib.metrics` directly (the former `utils_geo/graph/metric` compatibility shims have been removed).
- `regularized_zoning_experiments/run_regularized_search.py` is a thin CLI over `lib.regularized` for parameter-grid experiments; `evaluate_regularized.py` and `visualize_regularized_results.py` are analysis/figure scripts.
- `scripts/recompute_supply_fixed.py` is a diagnostics layer for stock-vs-flow supply metrics (Fix-1/Fix-2 are now upstreamed into `lib/supply.py`, so it is no longer the source of truth for those).

## Output layout

All generated data is git-ignored. Canonical locations (per active `study_area.active` scope):

- `data/raw/` — raw inputs (OSM edges/nodes, ring linework, order CSVs, POI CSV).
- `data/interim/<scope>/` — `road_edges_classified.gpkg`.
- `data/processed/<scope>/` — segment graph nodes/edges, POI/order features, baseline clusterings; plus stage outputs `order_pipeline/` (Stage 2) and `supply/` (Stage 3).
- `outputs/<scope>/` — legacy baseline pipeline products: `graphs/`, `tables/`, `figures/`.
- `regularized_zoning_experiments/runs/<run>/` — Stage 1 regularized run artifacts (`clusters/`, `tables/`, `figures/`); a `*_stage1_verify` sibling is written by `stage1_partition.py --verify`.
- `IntermediateDataForReproduce/` — the frozen reproducibility snapshot (never overwritten).
- `outputs/supply_audit*/` — optional outputs of the supply diagnostic scripts.

The three stage scripts write only under `data/processed/<scope>/`; `stage3_supply.py`'s output dir and parameters come from `config.yaml` (`stage3_supply`) with optional CLI overrides.

## Tests

`python -m pytest tests/` runs the suite (currently 18 tests): supply regressions incl. Fix-1 (natural-day clipping) and Fix-2 (origin-only fleet bound), the order-region end-to-end smoke test on a tiny fixture, and regularized-search numerics (`move_delta` equals the full objective difference; merge/split preserves K and connectivity). Tests load the migrated `lib.*` modules.

## Operational notes

- Run commands from the repository root; modules derive `PROJECT_ROOT` from their own path and expect `config.yaml` at that level. The whole codebase relies on `src/` being on `sys.path` (stage scripts insert it).
- Downstream stages expect upstream artifacts to exist; the frozen snapshot makes Stage 1's inputs/baselines and Stage 2's partition stable regardless of re-runs.
- Motor-vehicle filtering is controlled by `road_filter.keep_highway` / `exclude_highway` / `exclude_access_values` / `exclude_service_values`.
- GeoPackage writes go through normalization helpers because OSM columns can contain lists and names may contain characters unsuitable for GPKG fields.
- The order data is large (~12 GB) and Stage 2 outputs are multi-GB; a full Stage 2 re-run is a deliberate batch job, not a quick check.
