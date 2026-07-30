# RoadNet repository guidance

## Environment

Use only the existing `dydl` Conda environment:

```bash
conda run -n dydl pip install -e . --no-deps
conda run -n dydl python -m pytest
```

Do not use the system Python, add dependencies, change Demand spatial
assignment or introduce deterministic tie-breaking without a separately
approved phase.

## Supported workflow

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

The public CLI and `roadnet_partition` package are the only executable system.
New code must not import retired paths or inject `sys.path`.

## Ownership and privacy

- ordinary execution writes only to `outputs/runs/`;
- Golden is versioned, local-only regression data;
- publish is the only writer of stable `data/processed/<scope>/` products;
- reproduction export uses a separate privacy allowlist;
- never log or publish order/driver rows, coordinates, credentials or private
  payload contents.

Linux is the current Fifth Ring canonical. The Windows baseline remains a
private/local-only archive. Equal-distance nearest-neighbor assignment is not
guaranteed cross-platform; deterministic assignment v2 remains deferred.

Use split configs under `configs/`; paths resolve relative to the declaring
file. `configs/legacy/config.pre-refactor.yaml` is historical audit data only
and has no active reader. Do not edit a published scope in place.
