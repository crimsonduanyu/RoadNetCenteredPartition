# Phase 8 production path migration

The prior split-config audit identified 36 authoritative path comparisons.
Phase 8 re-audited all 36: 15 intentionally change asset ownership and 21 keep
their existing resolved target. Algorithm and scalar/list parameters do not
change; the 151 authoritative effective non-path values remain equivalent to
the root compatibility configuration.

## Migrated paths (15)

| Resolved field | Legacy target | Phase 8 target | Reason |
|---|---|---|---|
| `partition.inputs.graph` | mixed snapshot graph | `data/interim/fifth_ring/frozen_inputs/segment_relation_graph_road_poi_order.gpickle` | production input with provenance |
| `partition.inputs.relation_edges` | mixed snapshot CSV | `data/processed/fifth_ring/segment_relation_edges_road_poi_order.csv` | equal normal-data copy |
| `partition.inputs.classified_edges` | mixed snapshot GPKG | `data/interim/fifth_ring/road_edges_classified.gpkg` | equal normal-data copy |
| `partition.inputs.boundary` | mixed snapshot GPKG | `data/raw/beijing_fifth_ring_boundary.gpkg` | equal normal-data copy |
| `partition.inputs.segment_nodes` | mixed snapshot GPKG | `data/processed/fifth_ring/segment_nodes.gpkg` | equal normal-data copy |
| `partition.inputs.poi_features` | mixed snapshot CSV | `data/processed/fifth_ring/segment_poi_features.csv` | equal normal-data copy |
| `partition.inputs.order_features` | mixed snapshot CSV | `data/processed/fifth_ring/segment_order_features.csv` | equal normal-data copy |
| `partition.inputs.hourly_od` | mixed snapshot CSV | `data/processed/fifth_ring/segment_order_od_hourly.csv` | equal normal-data copy |
| `partition.inputs.baseline_clusters.louvain` | mixed snapshot GPKG | Golden v1 `legacy_comparisons/louvain/` | historical comparison, not canonical expected |
| `partition.inputs.baseline_clusters.leiden` | mixed snapshot GPKG | Golden v1 `inputs/` | active fixed initialization |
| `partition.inputs.baseline_clusters.demand_region_growing` | mixed snapshot GPKG | Golden v1 `legacy_comparisons/demand_region_growing/` | historical comparison, not canonical expected |
| `partition.contract.expected_partition` | implicit mixed snapshot canonical | Golden v1 `expected/partition/` | explicit canonical expected contract |
| `demand.order_pipeline.inputs.partition_gpkg` | mixed snapshot canonical | `data/processed/fifth_ring/partition/canonical_partition.gpkg` | published standalone fallback; full run binds same-run Partition |
| `tte.stage4_tte.distance.classified_edges_path` | mixed snapshot GPKG | `data/interim/fifth_ring/road_edges_classified.gpkg` | equal normal-data copy |
| `tte.stage4_tte.distance.partition_gpkg` | mixed snapshot canonical | `data/processed/fifth_ring/partition/canonical_partition.gpkg` | published standalone fallback; full run binds same-run Partition |

`stage1_partition.outputs.canonical_partition` remains a compatibility alias
for `partition.contract.expected_partition`; the new executor never writes to
that path.

## Unchanged path comparisons (21)

The project root, dataset config, dataset roots, run/output roots, Demand raw
orders/POI/relation inputs, Supply orders/cluster-index/output fallbacks, TTE
orders/cluster-index/precomputed-distance/representative/output fallbacks, and
TTE GraphML input retain their previous resolved targets. Dataset Fifth/Fourth
Ring raw/interim/processed roots also remain unchanged.

Root `config.yaml` is intentionally not rewritten: its paths remain a legacy
compatibility reference until the old entrypoints are removed in a later phase.
