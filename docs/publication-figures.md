# Publication figures

Figures are generated products under `outputs/figures/`; the repository does
not ship pre-rendered figures or depend on a published canonical partition.

```bash
conda run --prefix ./.conda/dydl python scripts/figures/best_partition_maps.py \
  --run outputs/runs/raw-only-reproduction \
  --boundary-from-manifest
conda run --prefix ./.conda/dydl python scripts/figures/raw_order_trip_time_distribution.py
```

`best_partition_maps.py` reads the selected Partition output and preparation
assets from the named run. The study-area boundary is an explicit input, never
discovered from the filesystem: pass `--boundary <path>`, or
`--boundary-from-manifest` to use the boundary the run recorded as its
`preparation.boundary` input (the recorded size and SHA-256 are re-verified, so
the figure always draws the run's own study area). Multi-layer `.gpkg` inputs
that hold more than one polygon layer require `--boundary-layer`; nothing is
chosen by filename or directory order. The contract and the refusal rules are
documented in
[docs/security/boundary-artifact-v1.md](docs/security/boundary-artifact-v1.md).
It writes PNG/PDF partition maps plus the two-panel partition and
mean-hourly-order figure. The trip-time histogram reads the raw June–August
order CSV. Both entrypoints preserve the recovered paper dimensions, DPI,
layers, typography, and vector PDF output where supported by Matplotlib.
