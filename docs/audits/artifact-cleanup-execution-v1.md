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

## Batch 4: reproducible Phase 5 validation payloads

- Deleted 18 large, ignored Phase 5 payload files totaling 2,476,512,934 bytes. All are reproducible from retained source inputs and code; tracked validation reports already record their contracts, aggregate results and logical hashes.
- Phase 5A source run `phase5a-full-v1`: deleted `cluster_od_10min.csv` (634,670,361 bytes, SHA-256 `5404b586587400cb9b22e4060fbfbef1391b5a51e77ce9a7002fdc5a0c522d06`) and `od_tensor_10min.npz` (68,784,851 bytes, `58c82fd01522c037f6588ff17fb221755373989711627ba464c44470cfd73cf3`).
- Phase 5B source run `legacy-historical-demand`: deleted `supply_available_floor.csv.gz` (4,663,612 bytes, `e4b96fd49d47b778f36834f432c3e805b7945564acf366a6210652c8e8d89e15`), `supply_fleet_lower_bound.csv.gz` (5,312,547 bytes, `d5d55c02f68f0da9e5ba78400441e2949641ca717bebfbda28e1c730aea5931b`), and `supply_inservice_od.csv.gz` (133,046,454 bytes, `292917a103c3cfde7f954609ca5b6869a3205a539343603f535c48c30548904a`).
- Phase 5B source run `new-historical-demand`: deleted the corresponding files (4,663,612 bytes, `9b476c12af305d6a27ffbfe45c448fb3b48b390be19ef0ce919d208933533938`; 5,312,547 bytes, `2ba6293600ba1aaadb14eedcc58f0b752bfa09035aee377bab1715aaa8882534`; 133,046,454 bytes, `8d36a1595b9275aea220619a8fd5f2e2425d29dcb361aaad2539f173914fe07d`).
- Phase 5C source runs `legacy-historical-demand` and `new-historical-demand`: deleted both copies of `TTE_count.parquet` (25,168,591 bytes each, `7e48c378773ea735f3cce2c82df38ddaf8679b7537d8ad7675db5899b069d638`), `TTE_hops.parquet` (27,467,136 bytes each, `21358909bf0edd4b911fd98960bd6e8301edb1e29a196d1f5fbc1ebaf99bb325`), `TTE_support.parquet` (55,498,610 bytes each, `a8fa61c5b435f895f0b2aa2364514f6749d1e88449538433643ccab64d642f56`), `TTE_raw.parquet` (85,814,602 bytes each, `ea4a4bf868194abeef83a8b56961ac21790a78b1cc011e4bee84ec0c93269f85`), and `TTE_imputed.parquet` (549,557,309 bytes each, `045dd9dbf14800f4f6bf6c2b92b3c0e26ee65a1179e8d9a6e28ecb83a5a283e7`).
- Retained Phase 5A assigned orders because `scripts/analysis/diagnose_demand_spatial_differences.py` is a current reader of that unique Linux baseline. Retained Phase 5A metadata, graph/index outputs, logs and aggregate spatial diagnostic.
- Retained Phase 5B configs, summaries and logs. Retained Phase 5C comparison JSON, contract JSON, runner, logs, representative nodes and network-distance inputs.
- Retained every file under `outputs/refactor-validation/pre-refactor/` and `outputs/refactor-validation/phase9/`, including the complete 4.1 GB Phase 9 full run and its manifests, validation reports and checksums.
- Retained the Windows historical baseline and source manifest, Golden tree, Linux canonical data, paper source inputs and all paper figures.
