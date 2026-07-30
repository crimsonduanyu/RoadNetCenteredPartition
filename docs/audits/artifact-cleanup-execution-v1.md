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

## Batch 3: legacy TTE and Supply gap visualizations

- Retained the three unknown TTE artifacts in place: `reports/figures/tte_trip_time_distribution.{png,pdf}` and `reports/tables/tte_raw_distribution_summary.csv`.
- Their SHA-256 values are `bf2fc5f48cfedf488eff899049465bd909ddbaa3945f121e0de440fb36a3431b`, `f5d44d33cd076bb57866366aa35f52da7448cf4db3f97e7cd92901cdc3bc102b`, and `ea817ad5c2177ffac87b1db9d1ea74a2b520e907cf0839f789ba133e1eaf98ba`; Git history identifies only their introduction in `97de198`, not a producer or source input.
- Archived five reproducible Supply gap diagnostics from `outputs/analysis/` to `artifacts/archive/supply-gap-diagnostics-v1/`: the cross-day heatmap/report and linear/log gap histograms/report.
- Archive bytes: 183,781; bytes deleted: 0.
- Producer scripts: `scripts/analysis/gap_distribution.py` and `scripts/analysis/gap_crossday_crosstab.py`; pre-migration SHA-256 values were `10f69d0c0aac37d38eae2f46d0ddd12278c34aaad2fa48f40c554f9dc9cea597` and `1bd9ab3679d27705eb40d98b9cb16ce7b847905a242520f1eac8f85fc5a0f9ca`.
- Read-only input: `data/processed/fifth_ring/order_pipeline/orders_region_assigned.csv.gz`, 2,693,844,002 bytes, SHA-256 `fb5b14e869e07cf11a8661650555b7a6f114566ef3d66167fbdd22ed26e4bb06`.
- Archived artifact SHA-256 values: `gap_crossday_crosstab.png` `6d6968a34c05d9a2aa146f4d62b472f303ed2fe744cf95f9142d13e13bfd8ebb`; `gap_crossday_crosstab_report.json` `21339700c9cf37a8c56e1aa50cb819cffdc80c742296d5c7450113bc0318df2a`; `gap_dist_by_hasnext.png` `0dbd8bf6da9980407fb7e9a41271f193b7e98cbe9b4bf153ccec776bfca153d1`; `gap_dist_by_hasnext_log.png` `e5ea4d592af9be19729fe7a9067e8ec1accb1d1fbb86255ffb1df31fd65d1f7a`; `gap_distribution_report.json` `99ab829b45bb16630e43622e0fe26f7d37c7b4fd9012c89ea3b24c4e0b73414f`.
- Repository search found no paper references to the gap artifacts; refactor inventories explicitly assign them Supply historical/diagnostic value and retain-analysis status.
