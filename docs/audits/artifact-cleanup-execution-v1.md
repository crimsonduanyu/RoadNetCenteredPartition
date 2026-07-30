# Artifact cleanup execution v1

## Batch 1: obsolete analysis outputs

- Deleted 12 unreferenced `outputs/analysis/*.json` probe and one-off verification results.
- Bytes released: 15,938.
- Repository search before deletion found only their inventory entries in `artifact-cleanup-proposal-v1.md`; no code, config, paper or operational documentation reader was found.
- No canonical, Golden, Windows baseline, validation evidence, paper figure, figure source input or unknown artifact was changed.

## Batch 2: duplicate Fourth/Fifth Ring aliases

- Recomputed SHA-256 for the Fourth/Fifth Ring outputs and confirmed 12 duplicate groups containing 32 redundant copies under the one-representative-per-hash rule.
- Deleted 31 aliases with no current code, config, documentation or paper reader.
- Bytes released: 122,377,087.
- Retained both `outputs/fourth_ring/figures/03_connector_compression_zoom_road_only_louvain.png` and `outputs/fifth_ring/figures/03_connector_compression_zoom_road_only_louvain.png`: their bytes match, but each is the named authority for a different study scope.
- Retained specific authority names for summaries, diagnostics, graphs and figures; removed only generic or redundant variant/algorithm aliases.
- Removed the two dormant generic table writes from `save_baseline_partition_outputs`; specific `cluster_summary_{variant}_{algorithm}.csv` and `road_name_split_diagnostics_{variant}_{algorithm}.csv` outputs remain unchanged.
- No unique artifact, canonical, Golden, Windows archive, Phase 9 run, paper figure or paper source input was changed.
