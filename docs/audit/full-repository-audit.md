# Full Repository Audit

Audit date: 2026-08-04

Audited HEAD: `5956ddf883bc355ef09c354acec3cb9c3b985ac1`

Scope: all 159 paths returned by `git ls-files`; this is a snapshot audit of the current repository, not a diff review.

## Executive summary

The repository has a coherent run-owned pipeline, explicit stage contracts, transactional publication, strong manifest/hash checks, and a substantial test suite. All 288 tests pass. The audit nevertheless confirmed **7 defects: P0 × 1, P1 × 2, P2 × 4, P3 × 0**.

The release export trust boundary is the immediate stop item: an accepted `--output` may be an ancestor of the project, and `--overwrite` then atomically replaces that directory and deletes its previous contents. A synthetic temporary-directory reproduction confirmed loss of an unrelated file. Until fixed, `export-reproduction --overwrite` must not be used. The publish path has a related validation gap: a crafted scope can place a staging copy outside `data/processed`; its final swap is currently rejected by a later prefix check, so the audit does **not** claim that publish can complete the same arbitrary replacement.

The two P1 issues affect normal experiment reliability. Preparation resume ignores configuration and input provenance and can silently reuse stale preprocessing products. Demand emits string/nullable identifiers that the directly bound Supply reader requires to be non-null integers, so valid alphanumeric identifiers fail the full pipeline.

Recommendation: do not freeze unrelated development, but immediately freeze reproduction exports with overwrite and canonical publication activity until AUD-001 is fixed and regression-tested. Resolve AUD-002 and AUD-003 before accepting new canonical experiment results.

## Repository architecture map

### Tracked-file inventory

| Class | Files | Role |
| --- | ---: | --- |
| Production package | 55 | `src/roadnet_partition/**` |
| Tests | 43 | `tests/**` |
| Analysis scripts | 6 | `scripts/analysis/**` and analysis entrypoints |
| Figure scripts | 3 | `scripts/figures/**` |
| Active configuration | 11 | dataset, preparation, pipeline, zoning and policy YAML |
| Legacy configuration | 2 | historical, explicitly non-runtime configuration |
| Documentation | 30 | public contracts, audit/performance records and operating instructions |
| Packaging/repository metadata | 9 | build, environment, license, citation and ignore metadata |
| **Total** | **159** | Complete `git ls-files` scope |

There are 106 tracked Python files (approximately 23,724 lines). No tracked symlinks, unusually large generated payloads, raw private datasets, credentials, or pre-rendered figure products were found.

### Public entrypoints and execution flow

- Package entrypoints: `python -m roadnet_partition` and the installed `roadnet-partition` console script, both routed to `roadnet_partition.cli:main`.
- CLI commands: `check-raw`, `run`, `validate`, `publish`, `export-reproduction`, and standalone `partition`, `demand`, `supply`, `tte` stages.
- Primary flow: raw data → optional run-owned preparation → Partition → Demand → Supply → TTE → validation → maintainer-only publication or privacy-filtered reproduction export.
- Figure entrypoints: the three scripts under `scripts/figures/`; development/analysis utilities remain outside the installed package.

### Module boundaries

| Boundary | Responsibility |
| --- | --- |
| `cli.py`, `__main__.py` | Public argument parsing, exit codes and command dispatch |
| `config.py`, `configs/**` | YAML schema validation, relative-path resolution and stage/dataset contracts |
| `pipeline/runner.py`, `worker.py`, `stages.py` | Fixed-stage orchestration, isolation, bindings, resume and output contracts |
| `pipeline/preparation.py` | Raw road/POI/order preprocessing and relation-graph construction |
| `roads/**`, `graphs/**`, `zoning/**` | Road segmentation, graph construction/distance, clustering/search/evaluation metrics |
| `downstream/**` | Demand assignment and checkpointing, Supply reconstruction, TTE matrices and contracts |
| `io/**` | Geospatial helpers, path ownership, atomic writes, manifests and environment checks |
| `pipeline/validation.py`, `publishing.py`, `releases/**` | Validation, canonical publish transaction and reproduction package export |
| `reporting/**`, `scripts/figures/**` | Publication-oriented rendering |

### Configuration, data and dependencies

Stage YAML resolves through a dataset YAML; declared paths are made absolute relative to the declaring file. Full-pipeline configuration binds formal outputs from each producer into the next consumer, overriding standalone fallback paths. Runs own `outputs/runs/<run-id>` and record config, inputs, outputs, Git state and selected runtime versions in `manifest.json`.

Inputs include GeoPackage/GraphML road data and CSV order/POI data. Intermediate and public outputs include GeoPackage, CSV/GZip CSV, Parquet, JSON/YAML and NetworkX pickle artifacts. External algorithm/I/O dependencies include GeoPandas, Shapely, PyProj, OSMnx, NetworkX, pandas, NumPy, SciPy, PyArrow, DuckDB, igraph, leidenalg, pymetis, python-louvain, scikit-learn, Fiona/Pyogrio/Rtree and Matplotlib.

Packaging uses setuptools from `pyproject.toml`; the supported installation is a Python 3.11 Conda environment from `environment.yml`, followed by editable installation. Pytest is the only configured automated checker. No tracked CI workflow, lint configuration, type-check configuration, Makefile or requirements file exists. `configs/legacy/**` and historical performance/refactor documents are non-runtime evidence; generated products belong under ignored `outputs/**`.

## Executed validation

Commands were taken from `README.md`, `docs/development.md`, `docs/installation.md`, `CONTRIBUTING.md` and `pyproject.toml` rather than guessed.

| Command | Result | Classification and audit impact |
| --- | --- | --- |
| `conda run --prefix ./.conda/dydl python -m pytest` | **PASS**: 288 passed, 77 warnings in 61.80 s | Full configured suite. Warnings are the known GeoPandas `unary_union` deprecation at `demand.py:280`; no test failure. |
| `conda run --prefix ./.conda/dydl python -m roadnet_partition --help` | **PASS** | Side-effect-free module-entry smoke test. |
| `conda run --prefix ./.conda/dydl roadnet-partition --help` | **PASS** | Installed console-entry smoke test. |
| `git diff --check` | **PASS** | Repository-requested whitespace/error check. |
| `conda run --prefix ./.conda/dydl pip install -e . --no-deps` | **NOT RUNNABLE** (exit 126) | Environment defect: `.conda/dydl/bin/pip` has a stale shebang pointing to missing `/home/dy/jupyter/workspace/RoadNetCenteredPartition-cleanroom/.conda/dydl/bin/python3.11`. This does not show a packaging-code failure, but clean install/build confidence is reduced. |

No official lint or type-check command is configured, so none was invented. A full production pipeline and `check-raw` were not run: tracked source intentionally excludes the private Beijing inputs, and the documented full run is roughly two hours/12 GiB. This limits full-scale data and performance validation, but does not affect the synthetic reproductions or static contract findings below.

## Confirmed findings

Findings are ordered by severity and then ID. The JSON companion contains exactly the same seven IDs.

### AUD-001 — P0 / high — Destructive export accepts project ancestors and publish staging can escape its owned root

**Locations:** `src/roadnet_partition/releases/reproduction.py:30-47`, `:243-245`, `:278-290`; `src/roadnet_partition/io/paths.py:92-152`; related publish path at `src/roadnet_partition/config.py:555`, `src/roadnet_partition/pipeline/runner.py:88`, and `src/roadnet_partition/pipeline/publishing.py:275-323`.

- **Trigger:** run `export-reproduction` on an exportable completed run with `--output` equal to a writable ancestor of the project (for example its workspace directory) and `--overwrite`. Separately, load a dataset/pipeline whose matching scope contains traversal or is absolute, then publish without `--dry-run`.
- **Actual impact:** `_safe_destination` rejects only exact protected paths and descendants of the run; it accepts ancestors. `transactional_scope_swap` then moves the ancestor to backup, installs the release, and recursively deletes the backup, destroying the project and unrelated sibling content. Crafted publish scopes can create and populate staging directories outside `data/processed`, leaving copies of stage products when the later swap-prefix check rejects the transaction. That can disclose private derived data and consume arbitrary writable storage. The later transaction check prevents the tested crafted publish scopes from completing an arbitrary target replacement; that narrower claim is a non-issue, not part of this P0.
- **Why tests missed it:** release tests use only child destinations under a temporary release root. Transaction tests check sibling/prefix/rollback mechanics but have no allowed-root concept. Config tests validate output filenames, not dataset or pipeline scope identifiers.
- **Minimal reproduction/static evidence:** in a synthetic temporary tree, `_safe_destination(workspace, run_dir, project_root)` returned the workspace. Passing that target and its valid sibling staging directory to `transactional_scope_swap(..., overwrite=True)` produced `old_unrelated_survived=False` and `replacement_present=True`. Static path checks also resolve `../../victim` outside `data/processed`; publish copies inventory at lines 307-310 before the swap rejects its malformed staging prefix.
- **Recommended direction:** validate every scope with the existing safe single-component identifier rule; resolve publish target/staging and assert both are direct children of the exact processed root before creating anything. Require reproduction exports to be descendants of an explicit release root, or at minimum reject any destination that is an ancestor, equal to, or inside project/run/data protected trees. Add an `allowed_parent`/owned-root invariant to the shared destructive swap API.
- **Regression test:** synthetic tests for absolute/traversal/empty scopes and export destinations equal to every ancestor/descendant boundary; assert rejection occurs before directory creation or copying and that marker files remain untouched. Keep existing rollback tests.

### AUD-002 — P1 / high — Preparation resume silently reuses outputs after config or input identity changes

**Locations:** `src/roadnet_partition/pipeline/runner.py:112-128`, `:150-163`; `src/roadnet_partition/pipeline/preparation.py:372-397`.

- **Trigger:** complete preparation, change a preparation parameter or a referenced preparation input, then invoke the same pipeline run with `--resume` while old preparation outputs and its manifest remain present.
- **Actual impact:** the pipeline fingerprint stores only the preparation config path, while the preparation reuse branch checks only existing output names and hashes against its old manifest. It never compares the current config record or current input records. Old road filtering, connector, POI/order-feature, similarity or Leiden products are silently reused and downstream stages consume them. Recorded config/input provenance can therefore disagree with the data that produced the result.
- **Why tests missed it:** resume tests exercise stage fingerprints and intact preparation outputs, but no test mutates the preparation YAML or one of its inputs between runs.
- **Minimal reproduction/static evidence:** a temporary preparation output set and manifest were created, then `connector_rules.max_connector_length_m` was changed. The config SHA changed, but `run()` printed `preparation: reused` and returned all old outputs unchanged. Lines 394-395 record config/inputs only after recomputation; lines 377-381 do not read them when deciding reuse.
- **Recommended direction:** include the preparation config content fingerprint in the pipeline fingerprint/external inputs and require exact equality of current config and input file records before reuse. On mismatch, invalidate preparation and all dependent formal stages with a clear resume-conflict reason.
- **Regression test:** parameterized resume tests that mutate (a) YAML content at the same path and (b) each input hash; both must reject reuse or recompute and invalidate Partition onward. An unchanged configuration must still reuse byte-identical outputs.

### AUD-003 — P1 / high — Demand output identifier schema is incompatible with the directly bound Supply reader

**Locations:** `src/roadnet_partition/downstream/demand.py:379-384`, `:452-458`; `src/roadnet_partition/downstream/order_checkpoints.py:47-69`; `src/roadnet_partition/downstream/supply.py:54-75`, `:722-738`; binding at `src/roadnet_partition/pipeline/stages.py:39-50`.

- **Trigger:** process a documented raw order whose `order_id` or `driver_id` is alphanumeric, or whose allowed `order_id` is null, then continue the normal Demand → Supply pipeline binding.
- **Actual impact:** Demand deliberately reads and writes both identifiers as Arrow/pandas strings and permits null `order_id`. Supply's only file reader forces both columns to `int64`. Valid Demand output therefore fails Supply with conversion errors, preventing a full normal pipeline for non-numeric identifiers; nullable order IDs fail for the same contract mismatch.
- **Why tests missed it:** Demand fixtures exercise strings, while Supply/file-integration fixtures use numeric identifiers. No cross-stage schema test feeds Demand's assigned-order artifact with alphanumeric/null IDs into the real Supply reader.
- **Minimal reproduction/static evidence:** a valid assigned-order CSV containing `order_id=o-1` and `driver_id=driver-A` passed the documented/Demand schema, but `load_orders()` raised `ValueError: invalid literal for int() with base 10: 'o-1'`. The pipeline binding sends `orders_region_assigned` directly to `stage3_supply.orders_path`.
- **Recommended direction:** make Supply consume the authoritative Demand checkpoint schema: string identifiers, nullable `order_id`, non-null trimmed `driver_id`. Convert only fields that are mathematically numeric; keep hashing/grouping deterministic for string driver IDs.
- **Regression test:** a real Demand-output-to-Supply-reader integration test with alphanumeric IDs, leading-zero IDs, null `order_id`, and invalid/null `driver_id`; assert preserved identity and unchanged Supply aggregates.

### AUD-004 — P2 / high — Network diameter is an inexact, hash-order-dependent double sweep on cyclic graphs

**Locations:** `src/roadnet_partition/zoning/metrics.py:330-358`; consumer at `src/roadnet_partition/zoning/evaluate.py:300-345`.

- **Trigger:** evaluate a connected cluster whose induced road graph contains cycles for which double-sweep Dijkstra is not an exact diameter algorithm.
- **Actual impact:** the metric may understate network diameter and can vary with Python hash seed because its start node is `list(component)[0]`. Evaluation's `diameter_not_worse` decision can therefore change across processes or misclassify a candidate. This is P2 because the current formal pipeline does not invoke the evaluation module, but the installed module and analysis path do.
- **Why tests missed it:** metric tests do not contain a cyclic counterexample or run the calculation under different hash seeds.
- **Minimal reproduction/static evidence:** for unit node lengths and edges `0-1, 0-2, 0-3, 1-2, 1-3`, the exact diameter is 2 (`2↔3`). Across `PYTHONHASHSEED` values, the implementation returned `2, 2, 1, 1, 2, 1, 1, 1`.
- **Recommended direction:** either compute exact weighted eccentricities for every node in each component, or explicitly rename/document a deterministic approximation and remove it from exact threshold decisions. Sort node selection if any approximation remains.
- **Regression test:** compare against `max(nx.all_pairs_dijkstra_path_length(...))` on trees and multiple cyclic counterexamples; repeat in subprocesses with several hash seeds.

### AUD-005 — P2 / high — Public artifact paths are loaded with unrestricted pickle deserialization

**Locations:** `src/roadnet_partition/zoning/partition.py:85-90`; `src/roadnet_partition/zoning/evaluate.py:38-43`; `src/roadnet_partition/reporting/best_partition_map.py:41-46`.

- **Trigger:** run standalone partition/evaluation or figure generation using a graph artifact supplied by another person, downloaded with a reproduction bundle, or replaced on disk.
- **Actual impact:** `pickle.load` executes attacker-controlled reduction code before any graph/type validation, yielding arbitrary code execution with the user's filesystem/data permissions.
- **Why tests missed it:** all pickle fixtures are created locally and trusted; tests assert graph semantics after deserialization and never exercise an untrusted payload.
- **Minimal reproduction/static evidence:** all three public loaders call `pickle.load(handle)` directly. Python pickle has no safe validation mode; object construction occurs before the subsequent node-type checks.
- **Recommended direction:** migrate portable graph artifacts to a non-executable format such as GraphML or a schema-validated node-link representation. If legacy pickle compatibility must remain temporarily, make it an explicit trusted-only opt-in with prominent CLI/documentation warnings and do not include it in shareable exports.
- **Regression test:** round-trip the replacement format with node/edge attributes and reject malformed schema, external references and legacy pickle unless an explicit compatibility flag is present.

### AUD-006 — P2 / high — Run manifests omit versions of result-determining dependencies and cannot identify untracked code content

**Locations:** `src/roadnet_partition/io/manifests.py:28-38`, `:112-149`; dependency declaration at `environment.yml:1-33`.

- **Trigger:** reproduce a run in an environment with different OSMnx, PyProj, igraph/leidenalg, pymetis/python-louvain, scikit-learn, Fiona/Pyogrio/Rtree or Matplotlib versions, or use `--allow-dirty` with changed untracked source content but unchanged path/status.
- **Actual impact:** identical commit, tracked diff/config and input hashes can produce different topology, projection, clustering, nearest-neighbor, serialization or rendering results without the manifest recording the responsible version. For untracked files, `diff_sha256` hashes only the status text/name because `git diff HEAD` contains no untracked bytes; changing their content without changing the name leaves the provenance digest unchanged.
- **Why tests missed it:** the lifecycle test only checks that NumPy appears in runtime metadata. No test compares the recorded distribution set to imported result-affecting dependencies or mutates untracked content.
- **Minimal reproduction/static evidence:** `DEPENDENCY_ALLOWLIST` contains only nine packages and omits the listed direct algorithm/I/O dependencies present in `environment.yml` and imports. `collect_git_info()` hashes `diff + NUL + status`; status for an untracked file is only `?? path`.
- **Recommended direction:** record all direct result-affecting distributions (preferably from installed project/environment metadata) plus relevant native/geospatial versions. Hash the bytes of untracked files when dirty runs are allowed, or forbid untracked code for publish/export.
- **Regression test:** assert a defined complete provenance set, verify unavailable packages are explicit, and show that changing bytes of the same untracked file changes the dirty-state fingerprint.

### AUD-007 — P2 / high — Figure CLIs always overlay the Fifth Ring boundary regardless of the selected run

**Locations:** `scripts/figures/best_partition_maps.py:20-39`; `scripts/figures/partition_order_panels.py:17-30`; fourth-ring public dataset at `configs/datasets/fourth_ring.yaml:3-49`.

- **Trigger:** pass a completed fourth-ring run (or any non-fifth-ring compatible run) to either figure CLI via its unrestricted `--run` argument.
- **Actual impact:** the script renders the selected run's partition and preparation edges against `data/raw/beijing_fifth_ring_boundary.gpkg`. The output therefore has the wrong study-area boundary or fails if only the run's configured boundary is available, producing a misleading publication figure.
- **Why tests missed it:** visualization tests call rendering helpers with explicit synthetic paths; they do not execute the CLI against manifests for multiple dataset scopes.
- **Minimal reproduction/static evidence:** both scripts resolve the run manifest/partition dynamically but pass the same hard-coded Fifth Ring path. The tracked fourth-ring dataset declares a different boundary at `configs/datasets/fourth_ring.yaml:46`.
- **Recommended direction:** record the preparation boundary as a run-owned input/output record and resolve it from the run manifest or resolved preparation config. If the scripts intentionally support only Fifth Ring, reject other scopes before rendering and state that restriction in `--help` and documentation.
- **Regression test:** CLI tests for fifth- and fourth-ring synthetic manifests must pass the corresponding boundary to the renderer; unsupported scopes must fail before creating output.

## Suspected risks (not confirmed findings)

- **SUS-001 — nearest-segment tie determinism:** `src/roadnet_partition/io/geospatial.py:336-349` drops duplicate `row_id` after `sjoin_nearest` without an explicit `seg_id` tie-break. Equidistant matches could depend on spatial-index ordering, but reversing segment order in the available environment still selected the same segment, so the audit could not demonstrate instability.
- **SUS-002 — wide Partition grid versus canonical pipeline:** `zoning.partition` intentionally supports multiple settings and outputs, while `pipeline.stages.canonical_partition_output_key` requires exactly one. The active canonical config is explicitly 1×1, so this is not a defect in the documented current run. A clearer early validation would prevent an expensive late failure if users widen the public list-valued config.

## Verified non-issues

- YAML uses `safe_load` or a `SafeLoader` subclass; no unsafe generic YAML loader was found.
- Subprocess calls use argument vectors and no `shell=True`; no confirmed shell-injection path was found.
- Publish inventory, run ownership, stage output allowlists, hashes, complete markers and rollback behavior are materially validated; the AUD-001 gap is target-root validation, not inventory authenticity.
- A suspected TTE `inf`-to-`int32` support conversion was disproved on the formal count path: every usable candidate has at least one finite current-leg support under the contract predicate.
- A suspected Supply nested-overlap idle-window error was disproved because Demand labels the whole overlap component as carpool and Supply merges it with a running maximum.
- The tested malformed publish scopes do not pass `transactional_scope_swap`'s sibling/prefix validation, so no arbitrary final publish replacement was demonstrated. They can still cause the pre-swap staging escape reported in AUD-001.
- Secret-pattern scanning found no tracked token/credential candidate. No tracked symlink or private raw-order artifact was found.

## Test coverage gaps

1. Destructive path tests do not model protected roots, ancestors, absolute/traversal scopes, or “reject before write” behavior.
2. Resume tests do not mutate preparation configuration or raw preparation inputs.
3. There is no real Demand-artifact → Supply-reader schema integration test.
4. Diameter metrics lack exact cyclic counterexamples and cross-hash-seed determinism checks.
5. Graph artifact tests assume trusted pickle and do not define a safe interchange schema.
6. Provenance tests cover only one dependency and omit untracked-content identity.
7. Figure CLI tests do not vary dataset scope/boundary.
8. The suite is broad but uses synthetic/tiny inputs; full-scale resource behavior and private-data schema variation remain externally dependent.

## Unverified scope and confidence

- A clean environment creation/editable install was not possible because the existing local `pip` launcher has an environment-specific stale shebang. Packaging metadata was inspected and both existing entrypoints worked, but a clean wheel/editable build remains unverified.
- No production Beijing run, canonical publish or real export was executed. The P0 reproduction used only disposable temporary directories, and all other reproductions used synthetic data.
- No official static linter/type checker or CI pipeline exists to run. Manual review covered all tracked production modules, tests, configuration, scripts and public-contract documentation, with targeted call-site searches and reproductions for high-severity candidates.
- Overall audit confidence is **high for the seven confirmed findings** and **medium-high for repository-wide absence claims**. Passing tests increase baseline confidence but do not cover the documented boundary cases.

## Audit-method note

This report deliberately excludes formatting/naming preferences, duplicate symptoms of one root cause, historical-only TODOs, and performance ideas without a demonstrated failure mode. Potential P0/P1 items were checked against all direct call sites, relevant tests, configuration/documentation constraints, a minimal reproduction, and plausible counterexamples before inclusion.
