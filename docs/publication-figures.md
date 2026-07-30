# Publication figures

Submission assets are tracked under `artifacts/paper/`. Their inputs, dimensions,
DPI, vector properties and SHA-256 values are recorded in
`artifacts/paper/figure_manifest.json` and `artifacts/paper/checksums.sha256`.

Render the canonical partition maps:

```bash
conda run -n dydl python scripts/figures/best_partition_maps.py
conda run -n dydl python scripts/figures/partition_order_panels.py
```

Render the raw order trip-time distribution:

```bash
conda run -n dydl python scripts/figures/raw_order_trip_time_distribution.py
```

These commands read the declared local inputs and write PNG/PDF pairs directly
to `artifacts/paper/figures/`. They do not run Partition, Demand, Supply, or TTE
and do not write `data/processed/`.

The two-panel paper map preserves the historical low-saturation, high-contrast
cluster palette on the left and a neutral-gray-to-saturated-blue mean hourly
order heatmap on the right. PNG outputs use 300 DPI; PDF map geometry and text
remain vector where supported.

The retained `tte_trip_time_distribution.{png,pdf}` pair is a unique historical
visualization of the formal TTE raw matrix. Its original producer was not
committed, so it is preserved rather than presented as a currently generated
figure.
