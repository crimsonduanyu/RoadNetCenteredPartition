# Pipeline

The fixed execution order is:

```text
partition → demand → supply → tte
```

Run the complete pipeline:

```bash
conda run --prefix ./.conda/dydl roadnet-partition run \
  --config configs/pipelines/full.yaml
```

Runs are owned directories under `outputs/runs/<run_id>/`. Resume or replace a
stage without writing the canonical scope directly:

```bash
conda run --prefix ./.conda/dydl roadnet-partition run \
  --config configs/pipelines/full.yaml \
  --run-dir outputs/runs/<run_id> \
  --from-stage supply --resume
```

Standalone commands are available for `partition`, `demand`, `supply`, and
`tte`; each requires its split YAML configuration.

Validation writes run-owned evidence unless `--report` is explicitly provided:

```bash
conda run --prefix ./.conda/dydl roadnet-partition validate \
  --run outputs/runs/<run_id> \
  --golden /path/to/external/golden
```

Publishing does not rerun algorithms. It validates and transactionally swaps a
complete scope into `data/processed/<scope>/`:

```bash
conda run --prefix ./.conda/dydl roadnet-partition publish \
  --run outputs/runs/<run_id> \
  --scope fifth_ring --overwrite \
  --baseline-decision configs/policies/fifth_ring_linux_canonical_v1.yaml \
  --dry-run
```

Generated diagnostics belong under `outputs/reports/`; validation campaigns
belong under `outputs/validation/`; reproduction packages belong under
`outputs/releases/`. All are disposable and Git-ignored.
