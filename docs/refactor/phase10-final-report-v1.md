# Phase 10 final refactor report v1

Date: 2026-07-30
Canonical source run: `20260730T020500Z-fifth-ring-full-02fce2f7`

## Outcome

Phase 10 retired the pre-refactor executable system without changing the
partition, Demand assignment, Supply/TTE semantics, published Linux canonical
payload, Windows archive payload, or Golden payload. The supported executable
surface is now the `roadnet_partition` package and `roadnet-partition` CLI.

The suggested protection tags `roadnet-linux-canonical-v1` and
`roadnet-refactor-pre-phase10` were absent. No tag was created, and no remote
push was performed.

## Commits and responsibilities

| Commit | Responsibility |
|---|---|
| `ad3d689` | inventory legacy entrypoints |
| `480513c` | retire legacy stage wrappers |
| `b9c0111` | retire legacy pipeline entrypoint |
| `5144b1f` | retire numbered and experiment entrypoints |
| `f508645` | retire migrated library bridges |
| `b6871bc` | archive legacy configuration |
| `36877a5` | retire mixed reproduction assets |
| `744e379` | prune obsolete analysis scripts |
| `0bc1768` | finalize repository documentation |
| `c8ea9b5` | verify final package boundaries |
| `a02f806` | allow retired archive-only Golden references while preserving payload checks |
| `d48af1a` | remove the final scratch notebook |
| `RoadNet: record final refactor acceptance` | final tree and acceptance evidence |

## Retired code and assets

Deleted executable entrypoints:

- `src/run_pipeline.py`;
- `src/stages/`;
- `src/lib/`;
- `src/env_setup.py`;
- numbered `src/00_*` through `src/05_*`;
- `src/adaptive_clustering.py`;
- `regularized_zoning_experiments/`;
- obsolete wrapper/config-reader tests and one-off analysis runners;
- scratch `tmp.ipynb`.

The old pipeline's migration-relevant behavior is retained in
`docs/history/pre-refactor-pipeline.md`. Asset-builder and Regularized
experiment conclusions are retained as documentation, not duplicate executable
implementations.

## Retained analysis tools

The following tools remain analysis-only and do not write the published scope:

- `scripts/analysis/compare_tte_outputs.py`;
- `scripts/analysis/diagnose_demand_spatial_differences.py`;
- `scripts/analysis/gap_crossday_crosstab.py`;
- `scripts/analysis/gap_distribution.py`;
- `scripts/analysis/tte_distribution_report.py`.

`tests/Make_Beijing_TTE.ipynb` remains a historical TTE method/reference
asset. It is not a pipeline or installed entrypoint.

## Compatibility and serialization

No compatibility package or legacy unpickling shim is retained. Text and
binary scans of tracked files, Golden, published data, the Windows archive,
runs and reproduction assets found no
`lib.metrics.MetricThresholds` or `lib.network_distance` pickle global.
The frozen relation graph identifies `networkx.classes.graph`.

The historical names `project_path` and `sort_cluster_ids` now exist only
as internal new-package helpers where used by current standalone distance/TTE
code. `load_project_config` is absent. No root-config reader remains.

Golden validation continues to require every local Golden payload and every
active external production input. A missing external asset is tolerated only
when the immutable manifest classifies it as `archive-only`; this permits the
retired mixed-directory README to disappear without weakening Golden payload
or contract validation.

## Legacy configuration

The root `config.yaml` was moved byte-for-byte to
`configs/legacy/config.pre-refactor.yaml`:

- SHA-256: `48dce0f8b92879e9bace985ca6af6a3a90fefe3ba3def4a9e997c7a02aeed65c`;
- status: historical audit data, no active reader;
- all 341 mapping keys have final classifications in
  `docs/refactor/config-key-map-v1.json`;
- counts: 244 new-authoritative-reader, 89 legacy-only, 8 historical-unread.

## IntermediateDataForReproduce

`IntermediateDataForReproduce/` was deleted after all 17 files were checked
against their classified migration targets. Production inputs, Golden expected
files and comparison assets had SHA-identical replacements. The immutable
Golden manifest/README retain historical source-path text by explicit static
allowlist.

## Documentation

`README.md`, `CLAUDE.md`, `configs/README.md`,
`.claude/settings.local.json` and the active TTE method document now describe
only the package/CLI workflow, `dydl`, lifecycle controls, transactional
publish, privacy export, Linux canonical status and Windows archive status.
Old three-stage and legacy command recommendations were removed.

## Static boundaries

The repository test scans text-bearing files and explicitly allowlists only
named immutable/historical evidence files. Active files contain no:

- `from lib` / `import lib`;
- `from src` / `import src`;
- `sys.path.insert` / `sys.path.append`;
- `src/stages`;
- `src/run_pipeline.py`;
- `IntermediateDataForReproduce`.

Package import-cycle, one-way layer, dynamic import and serialization boundary
tests also pass. Caches, bytecode, test caches, egg-info and empty retired
directories were removed after verification; editable installation may
regenerate ignored metadata locally.

## Tests, installation and CLI

- Baseline before retirement: `277 passed, 58 warnings`.
- Final suite: `244 passed, 55 warnings`.
- Focused Golden/static verification after the archive-only validation change:
  `12 passed, 2 warnings`.
- Editable reinstall:
  `pip uninstall -y roadnet-partition`, then
  `pip install -e . --no-deps`: passed.
- Repository-external import from `/tmp`: passed.
- Help checks passed for root, partition, demand, supply, tte, run, validate,
  publish and export-reproduction.

The lower test count reflects removal of tests that asserted obsolete wrappers
and bridges existed; retained tests cover algorithms, contracts, lifecycle,
direct/isolated execution, resume/overwrite, validation, transactional publish,
privacy export, Golden and canonical/archive decisions.

## Read-only canonical verification

The required command completed with `validation: passed`:

```bash
conda run -n dydl roadnet-partition validate \
  --run outputs/refactor-validation/phase9/full-runs/20260730T020500Z-fifth-ring-full-02fce2f7 \
  --golden artifacts/golden/beijing-fifth-ring-v1
```

No publish or algorithm execution occurred.

Published Fifth Ring scope:

- 30/30 allowlisted payload files match the size and SHA-256 records in
  `source_manifest.json`;
- `source_manifest.json` SHA-256:
  `8da62026df73a9e380fbab50c438df6ed7f19154bfbd101b989acdf7b3d1714d`;
- protected full-scope inventory SHA-256:
  `a5121da7f98be4773ad99d447c09c16ab62a6e255659eb8120a70d4d5059a788`;
- Linux canonical decision SHA-256:
  `cbeb6937d8f3485dda53766918870786e9a852e1b7da62ff17b1b20082df4202`.

Windows archive:

- 91/91 checksums passed;
- authoritative payload inventory SHA-256:
  `0815dc61d0e8ac950229372a4b0f7fae76adae8c410a1535328c112c8fad9540`;
- protected full-directory inventory SHA-256:
  `ea2561e577c31e2d676897227421abb95ba57e9cc24ce81d47c41f48cef6585f`;
- `checksums.sha256` SHA-256:
  `0a514588267ea4aa596b1e32d30f7aa0795622c845b31aec73e247740d003242`.

Golden:

- all eight payload checksums passed;
- manifest SHA-256:
  `b14da3822ad96d4f601dcd51350738f06d6b9a13203042be93064ed3a0856bf7`;
- `checksums.sha256` SHA-256:
  `22bccddd31ec007015f8f1476acc26c1d5698910fb995b14809d9be34e3219db`;
- protected full-directory inventory SHA-256:
  `28de46988c517b24585e57f03adb572f305195f9605cbc02ef380a9c73b6c23d`.

## Known limitations

- Equal-distance nearest-neighbor candidates are not guaranteed to resolve
  identically across platforms.
- Deterministic assignment v2 remains unimplemented.
- Windows archive and restricted/private data remain local-only.
- Historical refactor documents intentionally retain retired path names.
- No new feature, dependency or performance optimization was added in Phase 10.

## Final recommended commands

```bash
conda run -n dydl pip install -e . --no-deps
conda run -n dydl python -m pytest
conda run -n dydl roadnet-partition run --config configs/pipelines/full.yaml
conda run -n dydl roadnet-partition validate \
  --run outputs/runs/<run_id> \
  --golden artifacts/golden/beijing-fifth-ring-v1
```

Publishing remains an explicit, separately authorized transaction. Phase 10
does not perform a real publish.
