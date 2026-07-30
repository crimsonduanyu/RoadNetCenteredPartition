# Phase 10 removal inventory v1

Inventory date: 2026-07-30. The work tree was clean and `git diff --check`
passed before this inventory was created. The suggested protection tags
`roadnet-linux-canonical-v1` and `roadnet-refactor-pre-phase10` did not exist;
no tag was created and no remote operation was performed.

`reader` and `writer` describe the state before Phase 10. Documentation-only
references are retained where they explain migration history; executable and
test readers must be removed or redirected before the listed action occurs.

| Path | Current responsibility | New authoritative replacement | Current reader | Current writer | Test references | Documentation references | Historical value | Action |
|---|---|---|---|---|---|---|---|---|
| `src/env_setup.py` | environment compatibility bridge | `roadnet_partition.io.environment` | legacy numbered scripts | none | package-boundary/import tests | public-module migration inventory | bridge provenance only | delete |
| `src/lib/__init__.py` | legacy `lib` namespace | `roadnet_partition` package | legacy scripts and compatibility tests | none | compatibility tests | migration inventories | namespace only | delete |
| `src/lib/geo.py` | geospatial compatibility re-exports | `roadnet_partition.io.geospatial`, `roadnet_partition.roads` | numbered and visualization scripts | none | `test_geospatial_compat.py` | public-module inventory | bridge provenance only | delete |
| `src/lib/graph.py` | relation-graph compatibility re-exports | `roadnet_partition.graphs.relations` | relation graph builder | none | `test_graph_compat.py` | public-module inventory | bridge provenance only | delete |
| `src/lib/clustering.py` | cluster-count compatibility re-export | `roadnet_partition.zoning.algorithms.common` | old tests | none | clustering compatibility tests | public-module inventory | bridge provenance only | delete |
| `src/lib/network_distance.py` | distance compatibility re-exports | `roadnet_partition.graphs.distance` | `lib.tte_dataset`, old tests | none | `test_network_distance.py` alias assertion | public-module/config inventories | bridge provenance only | delete |
| `src/lib/metrics.py` | metrics compatibility re-exports and old pickle lookup | `roadnet_partition.zoning.metrics` | benchmark/visualization scripts, old tests | none | `test_metrics_compat.py` | public-module inventory | pickle lookup audit | delete |
| `src/lib/order_dataset.py` | Demand compatibility re-exports | `roadnet_partition.downstream.demand`, `roadnet_partition.graphs.build` | compatibility tests | none | Demand equivalence tests | Demand migration inventory | bridge provenance only | delete |
| `src/lib/regularized.py` | regularized compatibility re-exports | `roadnet_partition.zoning.regularized`, `roadnet_partition.zoning.partition` | experiment wrapper | none | regularized compatibility tests | zoning migration inventory | bridge provenance only | delete |
| `src/lib/supply.py` | Supply compatibility re-exports | `roadnet_partition.downstream.supply` | scratch analysis scripts and compatibility tests | none | Supply migration tests | Supply migration inventory | bridge provenance only | delete |
| `src/lib/tte_dataset.py` | TTE compatibility re-exports | `roadnet_partition.downstream.tte` | compatibility tests | none | TTE compatibility tests | TTE migration inventory | bridge provenance only | delete |
| `src/stages/__init__.py` | legacy stage namespace | public CLI | no production reader | none | package-boundary tests | old README/CLAUDE text | namespace only | delete |
| `src/stages/stage1_partition.py` | legacy Partition wrapper | `roadnet-partition partition` | compatibility tests | old run pipeline | stage/config compatibility tests | config map and migration docs | wrapper behavior summary | delete |
| `src/stages/stage2_demand.py` | legacy Demand wrapper | `roadnet-partition demand` | compatibility tests | old run pipeline | demand wrapper tests | migration docs | wrapper behavior summary | delete |
| `src/stages/stage3_supply.py` | legacy Supply wrapper and flags | `roadnet-partition supply` | compatibility tests | old run pipeline | Supply migration/config tests | migration docs | wrapper behavior summary | delete |
| `src/stages/stage4_tte.py` | legacy TTE wrapper | `roadnet-partition tte` | compatibility tests | old run pipeline | TTE migration/boundary tests | migration docs | wrapper behavior summary | delete |
| `src/run_pipeline.py` | sequential subprocess orchestration | `roadnet-partition run` | human shell entry only | none | old-boundary assertions | README/CLAUDE/history | migration behavior summary | archive-documentation |
| `src/00_download_osm.py` | one-off OSM harvest entry | frozen `data/raw` inputs and dataset config | no active reader | OSM/network raw assets | none | legacy config map | provenance only | archive-documentation |
| `src/01_preprocess_roads.py` | legacy road preprocessing entry | frozen normalized production inputs | no active reader | interim road assets | none | legacy config map | provenance only | archive-documentation |
| `src/02_build_order_features.py` | legacy order feature builder | frozen Partition inputs; Demand package for current assignments | no active reader | historical segment features | none | legacy config map | provenance only | archive-documentation |
| `src/02_build_poi_features.py` | legacy POI feature builder | frozen Partition inputs; Demand graph builder | no active reader | historical segment POI features | none | legacy config map | provenance only | archive-documentation |
| `src/02_build_segment_relation_graph.py` | legacy relation graph builder | frozen graph/edge inputs and `roadnet_partition.graphs` | no active reader | historical graph assets | none | legacy config map | provenance only | archive-documentation |
| `src/03_cluster_segments.py` | baseline algorithm compatibility entry | `roadnet_partition.zoning` | compatibility tests | old human entry | zoning compatibility tests | zoning migration inventory | bridge provenance only | delete |
| `src/04_visualize_clusters.py` | legacy partition visualization | existing reports/history; no formal pipeline responsibility | no active reader | ignored figures | none | visualization docs | conclusions only | archive-documentation |
| `src/05_benchmark_clusters.py` | legacy benchmark entry | `roadnet_partition.zoning.metrics` and existing benchmark docs | no active reader | ignored tables/reports | metrics tests target package implementation | benchmark docs | conclusions only | archive-documentation |
| `src/adaptive_clustering.py` | adaptive algorithm compatibility bridge | `roadnet_partition.zoning.algorithms.adaptive` | compatibility tests | none | adaptive compatibility tests | zoning migration inventory | bridge provenance only | delete |
| `regularized_zoning_experiments/run_regularized_search.py` | old search CLI | `roadnet-partition partition` | compatibility tests | human entry | regularized entrypoint tests | experiment plan/tracker | wrapper only | delete |
| `regularized_zoning_experiments/evaluate_regularized.py` | old evaluation CLI | `roadnet_partition.zoning.evaluate` | compatibility tests | human entry | evaluation compatibility tests | experiment plan/tracker | wrapper only | delete |
| `regularized_zoning_experiments/visualize_regularized_results.py` | duplicate report/visualization implementation | retained reports and history documentation | no active reader | ignored figures | visualization compatibility tests | experiment plan/tracker | conclusions/config provenance | archive-documentation |
| `regularized_zoning_experiments/config_v1.yaml` | Fourth Ring experiment configuration | historical summary | old experiment CLIs | manual | regularized tests | experiment plan | parameter provenance | archive-documentation |
| `regularized_zoning_experiments/config_v2.yaml` | Fourth Ring v2 experiment configuration | historical summary | old experiment CLIs | manual | regularized tests | experiment tracker | parameter provenance | archive-documentation |
| `regularized_zoning_experiments/config_v2_fifth_ring_lc1_lr1_k100.yaml` | Fifth Ring search configuration | `configs/zoning/regularized.yaml` plus history summary | old experiment CLIs | manual | regularized tests | experiment tracker | canonical migration provenance | archive-documentation |
| `regularized_zoning_experiments/EXPERIMENT_PLAN.md` | old experiment plan | `docs/history/pre-refactor-regularized-experiments.md` | human | manual | none | README/docs | useful migration context | archive-documentation |
| `regularized_zoning_experiments/EXPERIMENT_TRACKER.md` | old run tracker | `docs/history/pre-refactor-regularized-experiments.md` | human | manual | none | README/docs | useful migration context | archive-documentation |
| `regularized_zoning_experiments/runs/` | ignored experiment outputs | Golden, Phase 9 source run, Windows archive | no active reader after wrapper retirement | old experiment CLIs | none | Golden inventory | no unique payload after hash audit | delete |
| `config.yaml` | unified legacy configuration | split configs under `configs/` | legacy wrappers/scripts and compatibility audits | manual | config compatibility tests | config migration docs | all 341 keys retained for audit | retain-data |
| `IntermediateDataForReproduce/README.md` | description of mixed frozen directory | Golden/frozen-input/archive manifests and history | human | manual | Phase 8 migration test | Golden migration inventory | migration provenance | archive-documentation |
| `IntermediateDataForReproduce/*` | mixed production, Golden and comparison payload | SHA-identical files under `data/`, `artifacts/golden/` | root legacy config only | old builders/search | Phase 8 migration test | Golden inventory | no unique payload after hash audit | delete |
| `scripts/analysis/build_production_config_reports.py` | one-off Phase 6 config audit generator | final `config-key-map-v1.json` | no runtime reader | refactor docs | config audit tests indirectly | Phase 6 reports | generator no longer needed | delete |
| `scripts/analysis/step1_compare_daily_vs_fullmem.py` | one-off Supply comparison | retained Supply contracts/tests | no active reader | analysis JSON | none | Supply history | conclusion only | archive-documentation |
| `scripts/analysis/step1_fullmem_run.py` | scratch Supply writer using `lib` | public Supply CLI/package | no active reader | noncanonical processed scratch | none | Supply history | none beyond report | delete |
| `scripts/analysis/step1_logic_verify.py` | one-off Supply source loader | retained Supply tests | no active reader | analysis JSON | none | Supply history | conclusion only | archive-documentation |
| `scripts/analysis/step_chunk_full.py` | one-off Supply source loader | retained Supply tests | no active reader | analysis JSON | none | Supply history | conclusion only | archive-documentation |
| `scripts/analysis/step_chunk_verify.py` | one-off Supply source loader | retained Supply tests | no active reader | analysis JSON | none | Supply history | conclusion only | archive-documentation |
| `scripts/analysis/step_finalize_full.py` | one-off Supply source loader | retained Supply tests | no active reader | analysis JSON | none | Supply history | conclusion only | archive-documentation |
| `scripts/analysis/step_midnight_full.py` | one-off Supply source loader | retained Supply tests | no active reader | analysis JSON | none | Supply history | conclusion only | archive-documentation |
| `scripts/analysis/step_midnight_verify.py` | one-off Supply source loader | retained Supply tests | no active reader | analysis JSON | none | Supply history | conclusion only | archive-documentation |
| `scripts/analysis/step_tau_idle_verify.py` | one-off Supply source loader | retained Supply tests | no active reader | analysis JSON | none | Supply history | conclusion only | archive-documentation |
| `scripts/analysis/supply_block_mem_probe.py` | one-off Windows memory probe | no production replacement required | no active reader | analysis JSON | none | Supply history | measured conclusion only | archive-documentation |
| `scripts/analysis/supply_mem_probe.py` | one-off Windows memory probe | no production replacement required | no active reader | analysis JSON | none | Supply history | measured conclusion only | archive-documentation |
| `scripts/analysis/compare_tte_outputs.py` | read-only TTE comparison | same script using explicit inputs | human analysis | `outputs/analysis` only | none | analysis README | independent diagnostic value | retain-analysis |
| `scripts/analysis/diagnose_demand_spatial_differences.py` | read-only platform assignment diagnosis | same script using explicit inputs | human analysis | explicit report path only | dedicated diagnostics test | Phase 9 comparison docs | independent diagnostic value | retain-analysis |
| `scripts/analysis/gap_crossday_crosstab.py` | read-only Supply gap analysis | same script | human analysis | `outputs/analysis` only | none | Supply history | paper/diagnostic value | retain-analysis |
| `scripts/analysis/gap_distribution.py` | read-only Supply gap analysis | same script | human analysis | `outputs/analysis` only | none | Supply history | paper/diagnostic value | retain-analysis |
| `scripts/analysis/tte_distribution_report.py` | read-only trip-time distribution report | same script | human analysis | `reports/` only | none | TTE method docs | paper/reporting value | retain-analysis |

## Serialization decision

Text/binary scans of Git-tracked files, the Golden payload, the published
Fifth Ring scope, the Windows archive, the Phase 9 source run, reproduction
assets and ignored experiment outputs found no serialized
`lib.metrics.MetricThresholds` reference. The only production `.gpickle`
global is `networkx.classes.graph.Graph`. Therefore no legacy unpickling shim
is justified and the whole `src/lib` namespace can be removed.
