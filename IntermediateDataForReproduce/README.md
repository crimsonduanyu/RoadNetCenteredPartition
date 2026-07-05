# IntermediateDataForReproduce

Frozen snapshot of the artifacts needed to **reproduce the canonical spatial
partition deterministically**, isolated here so that re-running any pipeline
stage cannot overwrite them.

## Why this exists

The canonical partition used by all downstream work
(`segment_clusters_road_poi_order_regularized_leiden_lc1p0_lr1p0.gpkg`) is the
output of a regularized search that is **deterministic**, but it is initialized
from a **leiden** baseline that is **not bit-reproducible** (Leiden has random
seeding, and the upstream road graph depends on the live OSM download at the
time it was harvested). To guarantee the exact result can always be regenerated,
these inputs are frozen and `config.yaml` points the Stage 1 regularized step at
this folder instead of at the regenerable `data/` / `outputs/` locations.

## Contents and provenance

Copied verbatim (see git history of `config.yaml` for the move):

Regularized-search inputs (OSM/preprocessing-derived, frozen):
- `segment_relation_graph_road_poi_order.gpickle` ← `outputs/fifth_ring/graphs/`
- `segment_relation_edges_road_poi_order.csv`     ← `data/processed/fifth_ring/`
- `segment_nodes.gpkg`                             ← `data/processed/fifth_ring/`
- `segment_poi_features.csv`                       ← `data/processed/fifth_ring/`
- `segment_order_features.csv`                     ← `data/processed/fifth_ring/`
- `segment_order_od_hourly.csv`                    ← `data/processed/fifth_ring/`
- `road_edges_classified.gpkg`                     ← `data/interim/fifth_ring/`
- `beijing_fifth_ring_boundary.gpkg`               ← `data/raw/`

Baseline cluster products (random algorithms — the initializations / comparisons):
- `segment_clusters_road_poi_order_leiden_res0p6.{gpkg,csv}`          (leiden init)
- `segment_clusters_road_poi_order_louvain_res0p522.{gpkg,csv}`
- `segment_clusters_road_poi_order_demand_region_growing.{gpkg,csv}`

Canonical regularized output (consumed by Stage 2):
- `segment_clusters_road_poi_order_regularized_leiden_lc1p0_lr1p0.{gpkg,csv}`
  ← `regularized_zoning_experiments/runs/v2_fifth_ring_road_poi_order_lc1_lr1_k100/clusters/`

## Rules

- **Never overwrite** these files. Stage 1 re-runs write to
  `regularized_zoning_experiments/runs/...` and are compared against the frozen
  canonical output for verification, never written here.
- The binaries are git-ignored (large). Only this README is tracked. Keep the
  folder backed up alongside the repo if reproducibility must survive a reclone.
