# Pre-refactor pipeline

The retired pipeline entrypoint was `src/run_pipeline.py`. It launched four
standalone Python scripts in this fixed order:

```text
partition → demand → supply → tte
```

Each stage ran in a separate Python process. Partition additionally used a
legacy `--verify` flag. Every wrapper read the root `config.yaml` and wrote to
paths configured there; the driver had no run manifest, owned run directory,
transactional publish boundary, resume/overwrite invalidation, stage-range
selection, or formal input/output contracts.

The authoritative replacement is:

```bash
conda run -n dydl roadnet-partition run \
  --config configs/pipelines/full.yaml
```

The replacement preserves fixed ordering and isolated execution while adding
run ownership, resolved config snapshots, stage contracts, direct/isolated
execution, `from-stage`, `to-stage`, `resume`, `overwrite`, validation,
transactional publishing and privacy-filtered reproduction export. The old
source is intentionally not copied here because Git history already retains it.
