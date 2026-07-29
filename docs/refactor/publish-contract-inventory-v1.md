# Phase 7 publish and reproduction contract inventory v1

Only manifest logical keys listed below may leave a completed pipeline run.
`source_manifest.json`, release manifests, reports, and README files are
generated provenance documents rather than stage outputs.

| Stage | Logical key | Run-relative path | Published path | Publish | Reproduction | Classification / size | Contract | Historical formal name |
|---|---|---|---|---|---|---|---|---|
| Partition | single `cluster_gpkg_*` | `partition/clusters/*.gpkg` | `partition/canonical_partition.gpkg` | yes | minimal candidate | unknown; synthetic in tiny tests | yes | canonical regularized GPKG |
| Partition | matching `cluster_csv_*` | `partition/clusters/*.csv` | `partition/canonical_partition.csv` | yes | minimal candidate | unknown; synthetic in tiny tests | yes | canonical regularized CSV |
| Partition | `resolved_config` | `partition/resolved_config.yaml` | — | no | no; pipeline snapshot is used | run-only search configuration | indirectly | run artifact |
| Partition | `manifest` | `partition/tables/run_manifest.csv` | — | no | no | search intermediate | yes | run manifest table |
| Partition | `objective_trace` | `partition/tables/objective_trace.csv` | — | no | no | search intermediate | yes | objective trace |
| Demand | `cluster_index` | `demand/cluster_index.csv` | `order_pipeline/cluster_index.csv` | yes | minimal metadata candidate | derived/private for production | yes | same |
| Demand | `orders_region_assigned` | `demand/orders_region_assigned.csv.gz` | `order_pipeline/orders_region_assigned.csv.gz` | yes | never minimal; full private candidate | private, very large | yes | same |
| Demand | `cluster_od` | `demand/cluster_od_<slot>min.csv` | `order_pipeline/cluster_od_<slot>min.csv` | yes | full restricted candidate | private aggregate, large | yes | same |
| Demand | `od_tensor` | `demand/od_tensor_<slot>min.npz` | `order_pipeline/od_tensor_<slot>min.npz` | yes | full restricted candidate | private aggregate, large | yes | same |
| Demand | `metadata` | `demand/metadata.json` | `order_pipeline/metadata.json` | yes | minimal candidate | metadata; paths and aggregates only | yes | same |
| Demand | `road_graph_edges` | `demand/cluster_graph_road_edges.csv` | `order_pipeline/cluster_graph_road_edges.csv` | yes | full restricted candidate | derived network data | yes | same |
| Demand | `road_adjacency_raw` | `demand/cluster_graph_road_adjacency_raw.npz` | same under `order_pipeline/` | yes | full restricted candidate | derived network data | yes | same |
| Demand | `road_adjacency_normalized` | `demand/cluster_graph_road_adjacency_normalized.npz` | same under `order_pipeline/` | yes | full restricted candidate | derived network data | yes | same |
| Demand | `poi_graph_edges` | `demand/cluster_graph_poi_edges.csv` | same under `order_pipeline/` | yes | full private candidate | private POI-derived | yes | same |
| Demand | `poi_adjacency_raw` | `demand/cluster_graph_poi_adjacency_raw.npz` | same under `order_pipeline/` | yes | full private candidate | private POI-derived | yes | same |
| Demand | `poi_adjacency_normalized` | `demand/cluster_graph_poi_adjacency_normalized.npz` | same under `order_pipeline/` | yes | full private candidate | private POI-derived | yes | same |
| Demand | `distance_graph_edges` | `demand/cluster_graph_distance_edges.csv` | same under `order_pipeline/` | yes | full restricted candidate | derived location data | yes | same |
| Demand | `distance_adjacency_raw` | `demand/cluster_graph_distance_adjacency_raw.npz` | same under `order_pipeline/` | yes | full restricted candidate | derived location data | yes | same |
| Demand | `distance_adjacency_normalized` | `demand/cluster_graph_distance_adjacency_normalized.npz` | same under `order_pipeline/` | yes | full restricted candidate | derived location data | yes | same |
| Demand | `poi_features` | `demand/cluster_poi_features.csv` | `order_pipeline/cluster_poi_features.csv` | yes | full private candidate | private POI-derived | yes | same |
| Demand | `poi_category_mapping` | `demand/cluster_poi_category_mapping.csv` | same under `order_pipeline/` | yes | full private candidate | private POI-derived | yes | same |
| Supply | `inservice_od` | `supply/supply_inservice_od.csv.gz` | `supply/supply_inservice_od.csv.gz` | yes | full restricted candidate | driver-derived aggregate, large | yes | same; old aliases excluded |
| Supply | `available_floor` | `supply/supply_available_floor.csv.gz` | `supply/supply_available_floor.csv.gz` | yes | full restricted candidate | driver-derived aggregate, large | yes | same; old aliases excluded |
| Supply | `fleet_lower_bound` | `supply/supply_fleet_lower_bound.csv.gz` | `supply/supply_fleet_lower_bound.csv.gz` | yes | full restricted candidate | driver-derived aggregate, large | yes | same; old aliases excluded |
| Supply | `run_summary` | `supply/run_summary.json` | `supply/run_summary.json` | yes | minimal candidate | aggregate metadata | yes | same |
| Supply | `config_used` | `supply/config_used.json` | `supply/config_used.json` | yes | minimal candidate | paths and parameters only | yes | same |
| TTE | `network_distance` | `tte/cluster_network_distance.parquet` | `tte/cluster_network_distance.parquet` | yes | full restricted candidate | derived location matrix, large | yes | same |
| TTE | `representative_nodes` | `tte/cluster_representative_nodes.csv` | `tte/cluster_representative_nodes.csv` | yes | full restricted candidate | derived location data | yes | same |
| TTE | `tte_raw` | `tte/TTE_raw.parquet` | `tte/TTE_raw.parquet` | yes | full restricted candidate | order-derived matrix, large | yes | same |
| TTE | `tte_count` | `tte/TTE_count.parquet` | `tte/TTE_count.parquet` | yes | full restricted candidate | order-derived matrix, large | yes | same |
| TTE | `tte_support` | `tte/TTE_support.parquet` | `tte/TTE_support.parquet` | yes | full restricted candidate | order-derived matrix, large | yes | same |
| TTE | `tte_hops` | `tte/TTE_hops.parquet` | `tte/TTE_hops.parquet` | yes | full restricted candidate | order-derived matrix, large | yes | same |
| TTE | `tte_imputed` | `tte/TTE_imputed.parquet` | `tte/TTE_imputed.parquet` | yes | full restricted candidate | order-derived matrix, large | yes | same |

The publish allowlist is complete and fixed. Reproduction `minimal` includes
only generated provenance, resolved snapshots, the canonical Partition pair,
Demand metadata, and Supply summaries. Production classification remains
`unknown` unless a later phase supplies an explicit distribution decision;
`full` contains private/restricted candidates and therefore cannot be exported
without a future, explicit approval mechanism.

Phase 8 boundary: resolved configs, sanitized manifests, validation reports,
and explicitly classified run outputs remain release candidates. Raw inputs and
Golden payload are never imported into a release inventory. Demand, Supply, and
TTE production artifacts remain private/restricted; only synthetic fixtures may
become `minimal` payload without a separate distribution decision.
