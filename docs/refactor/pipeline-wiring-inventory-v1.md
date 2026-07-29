# Phase 6B pipeline wiring inventory v1

This inventory is derived from the Phase 6A resolved stage configs,
`formal_stage_outputs`, stage functions, and contracts. The Phase 6B runner uses
only the fixed bindings listed here.

| Producer | Logical output key | Consumer | Resolved-config field | Required | Standalone fallback | Pipeline behavior |
|---|---|---|---|---|---|---|
| Partition | the single `cluster_gpkg_*` output (`cluster_gpkg_regularized_leiden_lc1p0_lr1p0` in the production config) | Demand | `order_pipeline.inputs.partition_gpkg` | yes | configured canonical/frozen partition GPKG | replace with the completed Partition output; retain fallback in binding provenance |
| Demand | `orders_region_assigned` | Supply | `stage3_supply.orders_path` | yes | configured assigned-orders file | replace with the completed Demand output |
| Demand | `cluster_index` | Supply | `stage3_supply.cluster_index_path` | optional for standalone Supply, required in the full pipeline | absent means Supply derives a universe from assigned orders | inject the completed Demand cluster index so the Supply contract checks the exact universe |
| Demand | `orders_region_assigned` | TTE | `stage4_tte.inputs.orders_path` | yes | configured assigned-orders file | replace with the completed Demand output |
| Demand | `cluster_index` | TTE | `stage4_tte.inputs.cluster_index_path` | yes | configured cluster index | replace with the completed Demand output |
| Partition | the single `cluster_gpkg_*` output | TTE | `stage4_tte.distance.partition_gpkg` | required only when TTE recomputes network distance | configured partition GPKG | inject only when no precomputed distance pair is configured |

## Confirmed non-bindings

- Demand consumes the canonical Partition GPKG. It does not consume the
  canonical CSV separately; the GPKG contains the `seg_id` to `cluster_id`
  mapping used by assignment and graph construction.
- Supply consumes assigned orders and, in pipeline mode, the Demand cluster
  index. Demand `metadata.json` contains no required Supply field.
- TTE consumes assigned orders and the cluster index. Demand metadata and graph
  products are not TTE inputs.
- Production TTE currently uses the configured precomputed
  `network_distance_path` and `representative_nodes_path`. These are dataset/run
  external assets, so no Partition output is needed for production distance
  loading. If those fallbacks are absent, TTE uses `graphml_path`,
  `classified_edges_path`, and `partition_gpkg`; only the last field is bound
  from this run's Partition stage.

Every binding is resolved from the producer manifest logical key, after the
producer contract, `_SUCCESS`, output allowlist, and recorded SHA-256 are
verified. No consumer searches an upstream directory or infers an artifact from
its basename.
