# Publication figures

Figures are generated products under `outputs/figures/`; the repository does
not ship pre-rendered figures or depend on a published canonical partition.

```bash
conda run --prefix ./.conda/dydl python scripts/figures/best_partition_maps.py \
  --run outputs/runs/raw-only-reproduction
conda run --prefix ./.conda/dydl python scripts/figures/raw_order_trip_time_distribution.py
```

`best_partition_maps.py` reads the selected Partition output and preparation
assets from the named run. It writes PNG/PDF partition maps plus the two-panel
partition and mean-hourly-order figure. The trip-time histogram reads the raw
June–August order CSV. Both entrypoints preserve the recovered paper dimensions,
DPI, layers, typography, and vector PDF output where supported by Matplotlib.
