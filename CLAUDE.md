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

If `python-louvain` is unavailable through conda, install it with:

```bash
pip install python-louvain
```

Run the full pipeline from the repository root:

```bash
python src/run_pipeline.py
```

Run individual stages from the repository root:

```bash
python src/00_download_osm.py
python src/01_preprocess_roads.py
python src/02_build_segment_relation_graph.py
python src/03_cluster_segments.py
python src/04_visualize_clusters.py
```

VSCode can run the currently open script with the Run button using the workspace settings in `.vscode/`, which select the `bj_road_partition` conda interpreter.

There is currently no test suite, lint config, packaging config, or single-test workflow in the repository.

## Architecture overview

This is a Python geospatial pipeline for road-centered semantic partitioning of the drivable road network inside Beijing Fifth Ring Road. The key modeling choice is to keep ordinary road segments as graph nodes while compressing short OSM `*_link` connector roads into relation edges between adjacent ordinary segments.

Pipeline stages are intentionally sequential and file-based:

1. `src/00_download_osm.py` queries OSM in a configured harvest bbox, extracts Fifth Ring road segments by configured Chinese name patterns, polygonizes the ring to select the inner Beijing boundary, then downloads the drivable OSM network within that polygon.
2. `src/01_preprocess_roads.py` reads raw OSM edges plus the saved Fifth Ring boundary and ring linework, normalizes OSM list-like fields, projects to the configured metric CRS, strictly keeps roads inside the Fifth Ring plus overlapping named Fifth Ring segments, filters motor-vehicle roads with explicit highway/access/service rules, assigns stable `seg_id` values, and classifies each edge as `ordinary` or `connector` based on highway class and max connector length.
3. `src/02_build_segment_relation_graph.py` writes ordinary segments as graph nodes, builds weighted undirected relation edges for direct topological adjacency and connector-mediated adjacency, then boosts edge weights with same-road continuity signals: shared name, shared OSM way id, shared highway class, and small bearing difference.
4. `src/03_cluster_segments.py` applies Louvain clustering from `python-louvain` to the weighted NetworkX relation graph and writes clustered GeoPackage/CSV outputs plus diagnostic summary tables.
5. `src/04_visualize_clusters.py` renders the classification map, clustered road map, and connector-compression zoom figure.

Shared utilities are split by domain:

- `src/utils_geo.py` owns project paths, config loading, directory creation, CRS projection, OSM value normalization, road-name matching, ring polygon construction, boundary validation, bounds projection, bearing/angle helpers, and GeoPackage-safe column/value conversion.
- `src/utils_graph.py` owns relation-graph edge bookkeeping: canonical segment pairs, incident-node indexing, edge-record creation, and serialization of set-valued edge attributes.

## Configuration and artifacts

`config.yaml` is the central source for study-area geometry, CRS, road filters, connector heuristics, continuity weights, graph weights, Louvain parameters, and visualization settings. Prefer changing behavior through this file when the needed parameter already exists.

The scripts create required output directories automatically. Important intermediate and final artifacts are:

- `data/raw/`: Fifth Ring segments/boundary, raw OSM graph, raw edge/node GeoPackages.
- `data/interim/road_edges_classified.gpkg`: filtered and role-classified road edges.
- `data/processed/segment_nodes.gpkg`: ordinary segment graph nodes.
- `data/processed/segment_relation_edges.csv`: serialized weighted relation edges.
- `outputs/graphs/segment_relation_graph.gpickle`: NetworkX graph consumed by clustering and visualization.
- `data/processed/segment_clusters.gpkg` and `.csv`: clustered ordinary segments.
- `outputs/tables/`: cluster summary and road-name split diagnostics.
- `outputs/figures/`: generated map figures.

## Operational notes

- Run commands from the repository root; scripts derive `PROJECT_ROOT` from their own path and expect `config.yaml` at that level.
- The first stage uses live OSMnx downloads, so it depends on network availability and current OSM data/naming quality.
- Downstream stages expect upstream artifacts to exist; rerun earlier stages after changing boundary extraction, road filtering, connector rules, CRS, or graph construction logic.
- The preprocessing stage uses `study_area.inside_length_ratio_threshold`, `study_area.boundary_tolerance_m`, and `study_area.ring_overlap_tolerance_m` to prevent bbox leakage while retaining the Fifth Ring itself.
- Motor-vehicle filtering is controlled by `road_filter.keep_highway`, `road_filter.exclude_highway`, `road_filter.exclude_access_values`, and `road_filter.exclude_service_values`.
- GeoPackage writes go through normalization helpers where needed because OSM columns can contain lists and names may contain characters unsuitable for GPKG fields.
