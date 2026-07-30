# RoadNet-Centered Partition Pipeline

This project builds a fixed four-stage Beijing road-network data pipeline:

```text
partition → demand → supply → tte
```

The new runner, stage contracts, validation, transactional publishing, and
reproduction export are the recommended interfaces. Legacy scripts and the
root `config.yaml` remain available during the compatibility period but are no
longer the recommended workflow.

## Environment

Use the project `dydl` environment for every command:

```bash
conda run -n dydl pip install -e . --no-deps
conda run -n dydl python -m pytest
```

Linux is the currently supported and formally validated canonical platform.
Do not change Demand spatial assignment as part of ordinary operation.

## Full pipeline

```bash
conda run -n dydl roadnet-partition run \
  --config configs/pipelines/full.yaml
```

Runs are isolated by stage by default and write only beneath:

```text
outputs/runs/<run_id>/
├── manifest.json
├── resolved_config.yaml
├── resolved_configs/
├── logs/
├── partition/
├── demand/
├── supply/
└── tte/
```

Continue an existing run after validating completed upstream stages:

```bash
conda run -n dydl roadnet-partition run \
  --config configs/pipelines/full.yaml \
  --run-dir outputs/runs/<run_id> \
  --from-stage supply \
  --resume
```

Explicitly invalidate one stage and everything downstream:

```bash
conda run -n dydl roadnet-partition run \
  --config configs/pipelines/full.yaml \
  --run-dir outputs/runs/<run_id> \
  --from-stage demand \
  --overwrite
```

## Single-stage commands

Standalone execution remains available for diagnostics and controlled partial
runs. Each command still writes an owned run directory rather than modifying
stable processed data directly.

```bash
conda run -n dydl roadnet-partition partition --config configs/zoning/regularized.yaml
conda run -n dydl roadnet-partition demand --config configs/pipelines/demand.yaml
conda run -n dydl roadnet-partition supply --config configs/pipelines/supply.yaml
conda run -n dydl roadnet-partition tte --config configs/pipelines/tte.yaml
```

Demand standalone mode expects the published canonical partition under
`data/processed/<scope>/partition/`. Full pipeline runs instead bind the
same-run Partition output directly. Supply and TTE likewise receive same-run
Demand outputs through fixed bindings.

## Validate and Golden

Golden assets are versioned regression expectations under
`artifacts/golden/`; they are not production inputs or reproduction releases.
The Fifth Ring v1 contract freezes 59,096 segments, 100 clusters, EPSG:32650,
and the canonical label-invariant grouping hash.

```bash
conda run -n dydl roadnet-partition validate \
  --run outputs/runs/<run_id> \
  --golden artifacts/golden/beijing-fifth-ring-v1
```

Validation is read-only except for optional run-owned reports. Golden payload
is local-only, checksum-verified, and must not be modified in place; create a
new version directory for a new expected result.

## Publish

Publishing constructs and atomically swaps one complete
`data/processed/<scope>/` product. It never reruns algorithms.

Phase 9.1 transactionally published the current `data/processed/fifth_ring/`
from the approved Linux canonical run. A Fifth Ring replacement requires a
run-bound baseline decision:

```bash
conda run -n dydl roadnet-partition publish \
  --run outputs/runs/<run_id> \
  --scope fifth_ring \
  --overwrite \
  --baseline-decision configs/policies/fifth_ring_linux_canonical_v1.yaml \
  --dry-run
```

Real publish remains gated on a complete validated run, a clean Git tree, and
an approved decision matching the exact run and hashes. The historical Windows
canonical is preserved privately under
`artifacts/baselines/fifth-ring-windows-v1/payload/`; it is not Golden and is
not eligible for reproduction export.

Demand nearest-segment assignment can depend on underlying candidate order for
overlapping, equal-distance segments. This release accepts the validated Linux
result as canonical; it does not claim row-wise equality with historical
Windows Demand or with every platform. A deterministic assignment v2 would be
a separate algorithm and data-product version.

## Reproduction export

Inspect the fixed privacy allowlist without creating a release:

```bash
conda run -n dydl roadnet-partition export-reproduction \
  --run outputs/runs/<run_id> \
  --output releases/reproduction/<version> \
  --profile minimal \
  --dry-run
```

Demand, Supply, TTE, raw inputs, Golden payload, assigned orders, driver-level
records, and unknown-license assets are private/restricted by default. A
successful export does not grant public-distribution permission.

## Data boundaries

```text
data/                  production inputs and stable published products
artifacts/golden/      versioned regression validation assets
outputs/runs/          one execution and its provenance
releases/              separately allowlisted reproduction packages
```

These trees are not copied wholesale into one another. Publish is
`run → data/processed`; Golden is explicit regression validation; export is an
independent privacy-reviewed package.

## Configuration

- `configs/datasets/` owns scope, CRS, and normal data roots.
- `configs/zoning/regularized.yaml` owns Partition and its explicit Golden
  initialization/expected contract.
- `configs/pipelines/{demand,supply,tte}.yaml` owns standalone fallbacks.
- `configs/pipelines/full.yaml` owns the fixed four-stage composition.

All paths resolve relative to their containing configuration file. Algorithm
parameters remain equivalent to the legacy root configuration; Phase 8 changes
only audited asset paths.

## Legacy compatibility

`src/run_pipeline.py`, `src/stages/`, and root `config.yaml` remain intact for
comparison until final migration. They may still reference the read-only
legacy mixed asset directory. Phase 10 has not started; do not delete these
entrypoints, bridges, configurations, or historical directories.

The preprocessing and historical clustering scripts under `src/00_*` through
`src/05_*` remain legacy asset builders, not the recommended production entry.
Detailed algorithm notes live under `docs/`.
