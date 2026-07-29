# RoadNet repository guidance

## Required environment

Run project commands through the existing environment:

```bash
conda run -n dydl pip install -e . --no-deps
conda run -n dydl python -m pytest
```

Do not install deferred clustering dependencies or alter Demand spatial
assignment without a separately approved phase.

## Recommended workflow

The supported pipeline is fixed:

```text
partition → demand → supply → tte
```

```bash
conda run -n dydl roadnet-partition run --config configs/pipelines/full.yaml
conda run -n dydl roadnet-partition validate \
  --run outputs/runs/<run_id> \
  --golden artifacts/golden/beijing-fifth-ring-v1
conda run -n dydl roadnet-partition publish \
  --run outputs/runs/<run_id> --scope fifth_ring --dry-run
```

Before Phase 9, do not run the complete production pipeline or perform a real
publish. The Demand Windows/Linux assignment gate remains unresolved.

## Ownership boundaries

- `outputs/runs/`: owned execution directories only.
- `data/`: raw/interim production inputs and transactionally published stable
  products.
- `artifacts/golden/`: versioned regression inputs and expected results.
- `releases/`: independently allowlisted reproduction packages.

Golden is never a general downstream input directory and is never copied as a
whole into a release. Demand/Supply/TTE full-pipeline inputs come from fixed
same-run bindings. Standalone Demand uses the published canonical Partition.

The Fifth Ring Golden v1 payload is local-only and read-only. Metadata and
checksums are tracked; private/large payload is ignored. Updating an expected
result requires a new Golden version directory.

## Configuration

Use split configs under `configs/`. Paths resolve relative to the containing
file, not the current working directory. Root `config.yaml` remains only for
legacy wrappers and must not be deleted or treated as the new authority.

Algorithm values stay aligned with the legacy configuration. Phase 8 path
migrations are recorded in `docs/refactor/golden-migration-inventory-v1.md`.

## Compatibility boundary

The old `src/run_pipeline.py` and `src/stages/` entrypoints remain available for
comparison and may still reference the read-only legacy mixed directory through
root `config.yaml`. Do not remove old entrypoints or the old directory before
Phase 9.

New package code must not import `src`, `lib`, or legacy wrappers, mutate
Golden, write directly to stable processed data during ordinary runs, invoke
Git write commands, or bypass stage contracts.

## Privacy

Never write order/driver rows, coordinates, credentials, environment variables,
or private payload contents to manifests, logs, reports, or release metadata.
Paths, hashes, sizes, shapes, aggregate statistics, classifications, and stage
status are permitted. Real export rejects private, restricted, and unknown
assets unless a later explicit approval mechanism is introduced.
