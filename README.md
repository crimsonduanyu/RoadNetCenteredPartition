# Beijing Road-Centered Semantic Network Partitioning Prototype

This project builds a reproducible road-centered partitioning pipeline for configurable Beijing ring-road study areas, currently **inside the Fifth Ring Road** or **inside the Fourth Ring Road**.

The key modeling choice is:

- ordinary road segments are graph nodes;
- short connector roads such as OSM `*_link` ramps are compressed into relation edges between adjacent ordinary road segments.

The study area is **not** a bbox. The shared raw road network is the Fifth-Ring network in `data/raw/`; narrower scopes such as Fourth Ring reuse that raw network and apply a different boundary filter during preprocessing.

Switch the active scope in `config.yaml`:

```yaml
study_area:
  active: "fifth_ring"   # or "fourth_ring"
```

## Environment setup

Recommended conda workflow:

```bash
conda env create -f environment.yml
conda activate bj_road_partition
```

If `python-louvain` is unavailable through conda on your machine:

```bash
pip install python-louvain
```

## How to run

Run the full pipeline from the project root:

```bash
python src/run_pipeline.py
```

Or run each stage manually:

```bash
python src/00_download_osm.py
python src/01_preprocess_roads.py
python src/02_build_poi_features.py
python src/02_build_order_features.py
python src/02_build_segment_relation_graph.py
python src/03_cluster_segments.py
python src/04_visualize_clusters.py
```

## VSCode one-click run

Open this repository root folder in VSCode. The workspace config in `.vscode/` selects the `bj_road_partition` conda interpreter, so opening a script such as `src/04_visualize_clusters.py` and clicking Run should use the same environment as the command-line workflow.

Visualization scripts save PNG files under `outputs/<active_scope>/figures/`; they do not open a popup window.

## Pipeline overview

### 1. Prepare ring boundary and shared raw network

`src/00_download_osm.py`:

- reuses existing `data/raw/beijing_edges_raw.gpkg` when available;
- for `fifth_ring`, downloads the shared Fifth-Ring road network only if the shared raw files are missing;
- for `fourth_ring`, extracts Fourth Ring segments from the shared raw edges without re-downloading OSM;
- polygonizes the configured ring-road linework and selects the central Beijing boundary polygon.

Saved raw artifacts include:

- `data/raw/beijing_fifth_ring_segments.gpkg`
- `data/raw/beijing_fifth_ring_boundary.gpkg`
- `data/raw/beijing_fourth_ring_segments.gpkg`
- `data/raw/beijing_fourth_ring_boundary.gpkg`
- `data/raw/beijing_drive_within_fifth_ring.graphml`
- `data/raw/beijing_edges_raw.gpkg`
- `data/raw/beijing_nodes_raw.gpkg`

### 2. Classify ordinary vs connector segments

`src/01_preprocess_roads.py` filters motor-vehicle roads, enforces the active scope boundary, and classifies each edge as either:

- `ordinary`
- `connector`

The spatial filter keeps roads whose geometry lies almost entirely inside the active boundary polygon, plus named boundary ring segments that overlap the saved ring linework. Roads that merely intersect the boundary but mostly lie outside are discarded.

The motor-vehicle filter uses an explicit OSM `highway` allowlist plus access/service exclusions. Pedestrian, cycle, path, track, construction, and proposed road classes are excluded, and roads tagged with non-motor-vehicle access values such as `no`, `private`, `agricultural`, or `forestry` are removed. Service roads remain configurable but parking aisles, driveways, drive-throughs, and emergency-access service subtypes are excluded by default.

A segment is treated as a connector when it is an OSM `*_link` road and its length is below the configured threshold.

### 3. Build the segment relation graph

`src/02_build_poi_features.py` assigns 2017 Beijing POIs to 100m buffers around ordinary road segments and saves segment-level POI composition, density, entropy, and dominant type features.

`src/02_build_order_features.py` reads the October 2017 ride-hailing order CSV in chunks, keeps the configured one-week window, matches pickup/dropoff points to nearest ordinary road segments, and saves segment-level demand features plus segment OD pair counts.

`src/02_build_segment_relation_graph.py` creates three undirected graph variants where:

- each ordinary road segment is a node;
- direct adjacency becomes a relation edge;
- connector-mediated adjacency becomes a relation edge;
- same-road continuity strengthens the relation weight.
- POI and order similarity are added only on existing road relation edges.

The graph contains three relation types:

1. direct topological adjacency between road segments sharing an endpoint;
2. connector-mediated adjacency through short link/ramp segments;
3. same-road continuity relations that encourage consecutive segments belonging to the same named road or road corridor to stay in the same cluster.

The graph variants are:

- `road_only`
- `road_poi`
- `road_poi_order`

### 4. Cluster segments

`src/03_cluster_segments.py` applies the configured clustering algorithms to each weighted graph variant and saves cluster labels plus diagnostic and evaluation tables. The default experiment is a 3x4 cross comparison:

- graph variants: `road_only`, `road_poi`, `road_poi_order`
- algorithms: `louvain`, `leiden`, `skater`, `metis`

Louvain and Leiden use the configured resolution and random seed. SKATER and METIS use the configured fixed `target_clusters` value.

### 5. Visualize outputs

`src/04_visualize_clusters.py` creates:

- ordinary vs connector segment map with the active boundary outline;
- clustered road segment map with the active boundary outline for the configured default graph/algorithm pair;
- zoomed connector-compression illustration.

Pass a graph variant and algorithm to visualize another result:

```bash
python src/04_visualize_clusters.py road_poi_order leiden
```

## Connector compression

This prototype constructs a road-centered partitioning representation for ride-hailing applications. Ordinary road segments are treated as clustering units, while short connector roads such as ramps and OSM link roads are compressed into relationship edges between ordinary road segments. This avoids treating connectors as independent functional regions while preserving their role in network connectivity.

## Road continuity regularization

Same-road continuity is modeled as a soft weighting term rather than a hard constraint. When adjacent segments share the same road name, OSM way identity, highway class, or similar bearing, their graph relation is strengthened. This encourages contiguous road corridors to remain together during clustering while still allowing splits when topology suggests they should separate.

## Main outputs

Expected artifacts include:

- `data/raw/beijing_fifth_ring_segments.gpkg`
- `data/raw/beijing_fifth_ring_boundary.gpkg`
- `data/raw/beijing_fourth_ring_segments.gpkg`
- `data/raw/beijing_fourth_ring_boundary.gpkg`
- `data/raw/beijing_drive_within_fifth_ring.graphml`
- `data/raw/beijing_edges_raw.gpkg`
- `data/raw/beijing_nodes_raw.gpkg`
- `data/interim/<active_scope>/road_edges_classified.gpkg`
- `data/processed/<active_scope>/segment_nodes.gpkg`
- `data/processed/<active_scope>/segment_poi_features.csv`
- `data/processed/<active_scope>/segment_order_features.csv`
- `data/processed/<active_scope>/segment_order_od_pairs.csv`
- `data/processed/<active_scope>/segment_relation_edges_{variant}.csv`
- `data/processed/<active_scope>/segment_clusters_{variant}_{algorithm}.gpkg`
- `outputs/<active_scope>/graphs/segment_relation_graph_{variant}.gpickle`
- `outputs/<active_scope>/tables/cluster_summary_{variant}_{algorithm}.csv`
- `outputs/<active_scope>/tables/road_name_split_diagnostics_{variant}_{algorithm}.csv`
- `outputs/<active_scope>/tables/graph_variant_evaluation.csv`
- `outputs/<active_scope>/tables/graph_algorithm_evaluation.csv`
- `outputs/<active_scope>/tables/graph_algorithm_ranked_summary.csv`
- `outputs/<active_scope>/figures/01_ordinary_vs_connector_segments.png`
- `outputs/<active_scope>/figures/02_segment_clusters_{variant}_{algorithm}.png`
- `outputs/<active_scope>/figures/03_connector_compression_zoom_{variant}_{algorithm}.png`

## Validation signals

A successful run should print at least:

- number of candidate active ring segments found;
- number of boundary polygons generated before selection;
- chosen boundary polygon area;
- number of raw edges;
- number of edges retained by the active ring rule;
- number of additional active boundary edges retained;
- number of edges discarded outside the active ring;
- number of edges removed by highway class filter;
- number of edges removed by motor-vehicle access filter;
- number of service edges removed by service subtype filter;
- number of ordinary segments;
- number of connector segments;
- number of segment graph nodes;
- number of direct adjacency edges;
- number of connector-mediated edges;
- number of continuity-enhanced edges;
- number of POI-weighted and order-weighted edges per graph variant;
- number of clusters;
- graph variant x algorithm evaluation metrics;
- top 10 largest clusters by total road length.

## Known limitations

- Ring extraction depends on OSM road naming quality.
- Connector identification still uses a simple `*_link` plus length threshold heuristic.
- Continuity strengthening is local and depends on OSM naming quality.
- POI and order semantics are added as local edge-weight terms on existing road relation edges, not as long-range semantic edges.
- The current stage does not yet implement downstream OD prediction, dispatch simulation, or service-type ratios.
- The zoom view is still configured manually for inspection rather than selected automatically.
