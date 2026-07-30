# RoadNet-Centered Partition Pipeline

The supported Beijing road-network pipeline is:

```text
partition → demand → supply → tte
```

Linux is the current Fifth Ring canonical platform. The historical Windows
baseline is a private/local-only archive, not Golden and not a release asset.

## Install and test

Use the existing `dydl` Conda environment; do not use the system Python.

```bash
conda run -n dydl pip install -e . --no-deps
conda run -n dydl python -m pytest
```

## Full pipeline

```bash
conda run -n dydl roadnet-partition run \
  --config configs/pipelines/full.yaml
```

Ordinary runs write only beneath `outputs/runs/<run_id>/`. Stages are isolated
by default. Lifecycle controls include `--resume`, `--overwrite`,
`--from-stage`, `--to-stage` and `--isolate-stages`/`--no-isolate-stages`.

```bash
conda run -n dydl roadnet-partition run \
  --config configs/pipelines/full.yaml \
  --run-dir outputs/runs/<run_id> \
  --from-stage supply --resume

conda run -n dydl roadnet-partition run \
  --config configs/pipelines/full.yaml \
  --run-dir outputs/runs/<run_id> \
  --from-stage demand --overwrite
```

## Single-stage commands

```bash
roadnet-partition partition
roadnet-partition demand
roadnet-partition supply
roadnet-partition tte
```

Each command also accepts lifecycle/config options shown by its `--help`. It
writes an owned run directory, never the published scope directly.

## Validate, publish and export

Validation is read-only except for optional reports owned by the run:

```bash
conda run -n dydl roadnet-partition validate \
  --run outputs/runs/<run_id> \
  --golden artifacts/golden/beijing-fifth-ring-v1
```

Publish never reruns algorithms. It transactionally swaps a complete product
into `data/processed/<scope>/` after validation and policy checks:

```bash
conda run -n dydl roadnet-partition publish \
  --run outputs/runs/<run_id> \
  --scope fifth_ring \
  --overwrite \
  --baseline-decision configs/policies/fifth_ring_linux_canonical_v1.yaml \
  --dry-run
```

Reproduction export applies a separate privacy allowlist:

```bash
conda run -n dydl roadnet-partition export-reproduction \
  --run outputs/runs/<run_id> \
  --output releases/reproduction/<version> \
  --profile minimal \
  --dry-run
```

## Data rules

- ordinary runs write `outputs/runs/`;
- `validate` is read-only for algorithms and published data;
- `publish` transactionally writes `data/processed/<scope>/`;
- release export includes only privacy-allowlisted assets;
- Golden is regression-only;
- Linux is the current canonical;
- the Windows baseline is a private/local-only archive;
- equal-distance nearest-neighbor candidates are not guaranteed to resolve
  identically across platforms;
- deterministic assignment v2 is not implemented.

## Configuration and development

Authoritative runtime configuration is split across `configs/datasets/`,
`configs/zoning/` and `configs/pipelines/`; paths resolve relative to the file
that declares them. The unified pre-refactor configuration is archived under
`configs/legacy/` for audit only and has no active reader.

New code must import `roadnet_partition`, use `dydl`, honor run
ownership/contracts, and never write stable processed data except through the
publish transaction. `scripts/analysis/` is analysis-only.
