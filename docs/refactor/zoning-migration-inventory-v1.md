# Zoning migration inventory v1

This inventory records the Phase 4 source behavior before migration. It is a
code-reading record, not a new contract; the old implementations and Phase 0
assets remain the behavioral baseline.

## Source ownership and callers

| Legacy source | Definitions / responsibility | Known callers |
|---|---|---|
| `src/03_cluster_segments.py` | Louvain, Leiden, SKATER, METIS; algorithm registry; partition CSV/GPKG writes; cluster summaries, diagnostics, evaluation and ranking; `main()` | direct README/CLAUDE command; `src/05_benchmark_clusters.py` consumes its products |
| `src/adaptive_clustering.py` | `AdaptiveParams`, `AdaptiveContext`; demand/centroid loading; demand-weighted seed selection; multi-source and capacitated assignment; region growth; boundary improvement; relabeling | `src/03_cluster_segments.py`; direct imports in tests/tools are supported by the compatibility module |
| `src/lib/regularized.py` | objective/search dataclasses; graph, demand and baseline loading; objective components and move delta; merge/split/normalization/search; grid naming and construction; partition writes; experiment runner | Stage 1, regularized experiment runner/evaluator, tests |
| `src/stages/stage1_partition.py` | adapts unified config to regularized config; canonical setting naming; run/verify/verify-only behavior | `src/run_pipeline.py`; README/CLAUDE direct command |
| `regularized_zoning_experiments/run_regularized_search.py` | regularized YAML CLI and wildcard compatibility export | direct experiment command; tests import it |
| `regularized_zoning_experiments/evaluate_regularized.py` | benchmark rows, direction handling, Pareto flags and candidate-selection tables | direct experiment command; visualization consumes tables |
| `regularized_zoning_experiments/visualize_regularized_results.py` | balanced-score and best-run selection plus raster/vector report rendering | direct experiment command |

There are no module-level mutable caches. Import-time effects are environment
setup and project-root constant evaluation; optional algorithm dependencies are
loaded inside their runners. File reads and writes occur only inside called
functions or `main()`.

## Baseline and adaptive behavior that must remain exact

- Louvain reads `clustering.resolution` and `clustering.random_state` and passes
  them directly to `community_louvain.best_partition`.
- Leiden preserves NetworkX node and edge iteration order, then passes the same
  resolution and seed to `leidenalg.find_partition`.
- SKATER sorts connected components by descending size and first node, builds a
  minimum spanning tree using inverse weight, removes edges using the existing
  reverse `(dissimilarity, str(u), str(v))` ordering, and allocates labels in
  component/subregion order.
- METIS sorts nodes and neighbors, retains the integer edge/vertex scaling and
  current `recursive=False`, `contiguous=False` flags. METIS itself owns any
  library-internal nondeterminism.
- Adaptive code has no random generator. Determinism comes from explicit node,
  component, heap and candidate ordering. Demand, degree and string node IDs
  are existing tie-break inputs. `relabel_partition` numbers clusters by sorted
  old labels.
- Adaptive configuration is read from `clustering.target_clusters` and every
  current `clustering.adaptive.*` key. Missing order features produce zero
  demand; non-positive total demand falls back to one unit per graph node.
- Adaptive input paths are `order_features` and `segment_nodes`, resolved by the
  scope-path reader. The two public algorithms return mappings and do not write.

## Baseline partition products

`03_cluster_segments.py` reads `segment_nodes`, each configured graph pickle and
edge CSV. For every graph/algorithm pair it writes
`segment_clusters_<variant>_<algorithm>.{gpkg,csv}`, summary and road-name
diagnostic CSVs. GeoPackage driver, inherited CRS/schema, CSV row order and
`cluster_id` mapping are preserved. The configured default Louvain pair also
writes the four historical unqualified filenames. Evaluation and ranked table
column construction/order remain unchanged.

## Regularized numerical behavior

- Objective terms are capacity, continuity cut, connector cut and road-integrity
  cut. Capacity uses the configured hinge loss and fixed target-cluster demand;
  the final objective preserves the existing multiplication and addition order.
- `EPS = 1e-12`, dataclass fields/defaults, loop order, sorted node/cluster
  choices, connectivity checks, candidate ranking, merge/split triggers,
  tie-break tuples, exception text and trace row construction are migration
  invariants.
- Search initialization is loaded from `inputs.baseline_clusters` according to
  `search.initializations`. Graph, segment node/edge, demand and output paths are
  read from the regularized config. No Phase 4 reader changes parameter values.
- Grid order is the existing `itertools.product` order. `setting_id`,
  `legacy_setting_id`, `regularized_algorithm_name` and
  `baseline_for_algorithm` remain byte-for-byte naming authorities.
- `run_from_config` writes cluster GPKG/CSV, per-run YAML, objective trace and
  run-manifest tables beneath the caller-provided run root. Canonical Stage 1
  selection remains the exact setting stem constructed from unified config;
  verification compares label-invariant segment groupings with the frozen file.

## Evaluation and reporting boundary

`evaluate_regularized.py` owns numerical benchmark rows, metric directions,
Pareto membership and candidate-selection tables. The visualization script owns
balanced-score calculation and best-run selection in addition to rendering;
those numerical selectors migrate to `zoning.regularized.selection`, while map,
palette, canvas and chart functions remain report-only compatibility code in
Phase 4. Rendering must consume existing artifacts and must not run partitioning.

## Configuration evidence

The authoritative Phase 4 readers will be recorded alongside the existing
legacy readers in `docs/refactor/config-key-map-v1.json`; no key is removed and
`config.yaml` is unchanged. Relative-path semantics of legacy entrypoints remain
project-root based. New internal partition calls require an explicit output
root and do not write to `data/processed`, Golden, or release locations by
default.
