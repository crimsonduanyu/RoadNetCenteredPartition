# Fifth-ring publish transition v1

Phase 9.1 replaces the complete `data/processed/fifth_ring/` scope. The pre-publish Windows canonical contains 91 files (7,588,535,926 bytes); the Linux run publish inventory contains 30 allowlisted files (4,310,941,229 bytes). Because two canonical Partition paths are new, the exact set transition is 28 retained paths, 63 removed paths, and 2 added paths—not a literal 61-path subtraction.

The audit found five genuine production inputs among the 63 removed paths. They were materialized without content changes under `data/interim/fifth_ring/frozen_inputs/` and the authoritative split configs now point there. Thus no new CLI execution depends on a path removed from the formal scope. Compatibility wrappers and root `config.yaml` remain unchanged and can use the private Windows archive during the compatibility period.

## Retained formal paths (28)

- `order_pipeline/cluster_graph_distance_adjacency_normalized.npz`
- `order_pipeline/cluster_graph_distance_adjacency_raw.npz`
- `order_pipeline/cluster_graph_distance_edges.csv`
- `order_pipeline/cluster_graph_poi_adjacency_normalized.npz`
- `order_pipeline/cluster_graph_poi_adjacency_raw.npz`
- `order_pipeline/cluster_graph_poi_edges.csv`
- `order_pipeline/cluster_graph_road_adjacency_normalized.npz`
- `order_pipeline/cluster_graph_road_adjacency_raw.npz`
- `order_pipeline/cluster_graph_road_edges.csv`
- `order_pipeline/cluster_index.csv`
- `order_pipeline/cluster_od_10min.csv`
- `order_pipeline/cluster_poi_category_mapping.csv`
- `order_pipeline/cluster_poi_features.csv`
- `order_pipeline/metadata.json`
- `order_pipeline/od_tensor_10min.npz`
- `order_pipeline/orders_region_assigned.csv.gz`
- `supply/config_used.json`
- `supply/run_summary.json`
- `supply/supply_available_floor.csv.gz`
- `supply/supply_fleet_lower_bound.csv.gz`
- `supply/supply_inservice_od.csv.gz`
- `tte/TTE_count.parquet`
- `tte/TTE_hops.parquet`
- `tte/TTE_imputed.parquet`
- `tte/TTE_raw.parquet`
- `tte/TTE_support.parquet`
- `tte/cluster_network_distance.parquet`
- `tte/cluster_representative_nodes.csv`

## Added canonical paths (2)

- `partition/canonical_partition.csv`
- `partition/canonical_partition.gpkg`

## Removed from formal scope (63)

| Old path | Old reader/use | New package reader | Disposition |
|---|---|---|---|
| `order_pipeline/cluster_od_15min.csv` | historical Demand consumers/tests | no; formal Demand uses fixed 10-minute outputs | historical Demand artifact; preserved in `fifth-ring-windows-v1` archive |
| `order_pipeline/od_tensor_15min.npz` | historical Demand consumers/tests | no; formal Demand uses fixed 10-minute outputs | historical Demand artifact; preserved in `fifth-ring-windows-v1` archive |
| `order_pipeline/orders_region_assigned_sample_200k.parquet` | historical Demand consumers/tests | no; formal Demand uses fixed 10-minute outputs | historical Demand artifact; preserved in `fifth-ring-windows-v1` archive |
| `poi_category_mapping.csv` | legacy preprocessing/graph builders | no formal new pipeline reader | legacy preprocessing artifact; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_demand_network_voronoi.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_demand_network_voronoi.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_demand_region_growing.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_demand_region_growing.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_leiden.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_leiden.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_louvain.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_louvain.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_metis.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_metis.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_skater.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_only_skater.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_demand_network_voronoi.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_demand_network_voronoi.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_demand_region_growing.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_demand_region_growing.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_leiden.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_leiden.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_louvain.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_louvain.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_metis.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_metis.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_demand_network_voronoi.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_demand_network_voronoi.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_demand_region_growing.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_demand_region_growing.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_leiden.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_leiden.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_leiden_res0p6.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_leiden_res0p6.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_louvain.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_louvain.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_louvain_res0p522.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_louvain_res0p522.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_metis.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_metis.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_skater.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_order_skater.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_skater.csv` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_clusters_road_poi_skater.gpkg` | legacy partition wrappers/comparison notebooks | no; canonical Partition is partition/canonical_partition.* and comparisons are Golden/archive | legacy comparison or old filename; preserved in `fifth-ring-windows-v1` archive |
| `segment_nodes.gpkg` | Partition/Demand production input | yes; switched to data/interim/fifth_ring/frozen_inputs | production-input relocated before publish; preserved in `fifth-ring-windows-v1` archive |
| `segment_order_features.csv` | Partition/Demand production input | yes; switched to data/interim/fifth_ring/frozen_inputs | production-input relocated before publish; preserved in `fifth-ring-windows-v1` archive |
| `segment_order_od_hourly.csv` | Partition/Demand production input | yes; switched to data/interim/fifth_ring/frozen_inputs | production-input relocated before publish; preserved in `fifth-ring-windows-v1` archive |
| `segment_order_od_pairs.csv` | legacy preprocessing/graph builders | no formal new pipeline reader | legacy preprocessing artifact; preserved in `fifth-ring-windows-v1` archive |
| `segment_poi_features.csv` | Partition/Demand production input | yes; switched to data/interim/fifth_ring/frozen_inputs | production-input relocated before publish; preserved in `fifth-ring-windows-v1` archive |
| `segment_relation_edges.csv` | legacy preprocessing/graph builders | no formal new pipeline reader | legacy preprocessing artifact; preserved in `fifth-ring-windows-v1` archive |
| `segment_relation_edges_road_only.csv` | legacy preprocessing/graph builders | no formal new pipeline reader | legacy preprocessing artifact; preserved in `fifth-ring-windows-v1` archive |
| `segment_relation_edges_road_poi.csv` | legacy preprocessing/graph builders | no formal new pipeline reader | legacy preprocessing artifact; preserved in `fifth-ring-windows-v1` archive |
| `segment_relation_edges_road_poi_order.csv` | Partition/Demand production input | yes; switched to data/interim/fifth_ring/frozen_inputs | production-input relocated before publish; preserved in `fifth-ring-windows-v1` archive |
| `supply/driver_chains.csv.gz` | legacy Supply scripts/debugging | no; five-file Supply contract is retained | legacy intermediate, log, partial, or old filename; preserved in `fifth-ring-windows-v1` archive |
| `supply/idle_windows.csv.gz` | legacy Supply scripts/debugging | no; five-file Supply contract is retained | legacy intermediate, log, partial, or old filename; preserved in `fifth-ring-windows-v1` archive |
| `supply/run.log` | legacy Supply scripts/debugging | no; five-file Supply contract is retained | legacy intermediate, log, partial, or old filename; preserved in `fifth-ring-windows-v1` archive |
| `supply/run_summary.partial.json` | legacy Supply scripts/debugging | no; five-file Supply contract is retained | legacy intermediate, log, partial, or old filename; preserved in `fifth-ring-windows-v1` archive |
| `supply/supply_available_by_cluster.csv.gz` | legacy Supply scripts/debugging | no; five-file Supply contract is retained | legacy intermediate, log, partial, or old filename; preserved in `fifth-ring-windows-v1` archive |
| `supply/supply_in_service_od.csv.gz` | legacy Supply scripts/debugging | no; five-file Supply contract is retained | legacy intermediate, log, partial, or old filename; preserved in `fifth-ring-windows-v1` archive |
| `supply/trip_segments.csv.gz` | legacy Supply scripts/debugging | no; five-file Supply contract is retained | legacy intermediate, log, partial, or old filename; preserved in `fifth-ring-windows-v1` archive |
| `supply_run.log` | legacy Supply scripts/debugging | no; five-file Supply contract is retained | legacy intermediate, log, partial, or old filename; preserved in `fifth-ring-windows-v1` archive |

## Complete Linux publish allowlist (30)

- `order_pipeline/cluster_graph_distance_adjacency_normalized.npz`
- `order_pipeline/cluster_graph_distance_adjacency_raw.npz`
- `order_pipeline/cluster_graph_distance_edges.csv`
- `order_pipeline/cluster_graph_poi_adjacency_normalized.npz`
- `order_pipeline/cluster_graph_poi_adjacency_raw.npz`
- `order_pipeline/cluster_graph_poi_edges.csv`
- `order_pipeline/cluster_graph_road_adjacency_normalized.npz`
- `order_pipeline/cluster_graph_road_adjacency_raw.npz`
- `order_pipeline/cluster_graph_road_edges.csv`
- `order_pipeline/cluster_index.csv`
- `order_pipeline/cluster_od_10min.csv`
- `order_pipeline/cluster_poi_category_mapping.csv`
- `order_pipeline/cluster_poi_features.csv`
- `order_pipeline/metadata.json`
- `order_pipeline/od_tensor_10min.npz`
- `order_pipeline/orders_region_assigned.csv.gz`
- `partition/canonical_partition.csv`
- `partition/canonical_partition.gpkg`
- `supply/config_used.json`
- `supply/run_summary.json`
- `supply/supply_available_floor.csv.gz`
- `supply/supply_fleet_lower_bound.csv.gz`
- `supply/supply_inservice_od.csv.gz`
- `tte/TTE_count.parquet`
- `tte/TTE_hops.parquet`
- `tte/TTE_imputed.parquet`
- `tte/TTE_raw.parquet`
- `tte/TTE_support.parquet`
- `tte/cluster_network_distance.parquet`
- `tte/cluster_representative_nodes.csv`

## Reader conclusion

- `configs/zoning/regularized.yaml`, `configs/datasets/fifth_ring.yaml`, and `configs/pipelines/demand.yaml` now read the five relocated production inputs from `data/interim/fifth_ring/frozen_inputs/`.
- Supply and TTE standalone fallbacks read only paths present in the 30-file formal inventory.
- Runtime full-pipeline bindings continue to use same-run Partition and Demand outputs.
- No publish rule copies an old stage directory or a legacy intermediate.
