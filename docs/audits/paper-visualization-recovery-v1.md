# Paper visualization recovery audit v1

Date: 2026-07-30
Historical source: `d2139c63a01e5506f190c03da2db5cdc2e79d495`

Correction: the initial audit incorrectly treated independent single-map and histogram PNG/PDF exports as the requested target. The actual target is the recovered horizontal two-panel figure described below.

## Correct target

`partition_and_mean_hourly_orders.{png,pdf}` contains:

- left: the canonical 100-cluster partition, using low-saturation categorical colors assigned with the historical adjacency-contrast ordering so neighboring clusters remain visually distinct;
- right: a spatial heatmap of mean hourly origin-order volume within each cluster, calculated over all 120 hourly slots in the frozen canonical hourly OD input, including zero-order cluster/hour combinations; low values use visible neutral gray and transition to increasingly saturated dark blue as volume rises;
- identical boundary, extent and road geometry across both panels, with a continuous order-volume colorbar on the right.

The composite two-panel caller itself is not present in the priority commit, its notebooks, or its tracked images. It was reconstructed from the commit's adjacency-aware color logic, current authoritative aggregation code and the author's clarified panel semantics. No other commit supplied substitute plotting code.

## Historical code found

The priority commit contains three same-figure PNG/PDF exports:

| Historical path | Function / adjacent caller | Historical outputs |
|---|---|---|
| `scripts/analysis/tte_distribution_report.py` | `plot_histogram` | `reports/figures/raw_order_trip_time_distribution.{png,pdf}` |
| `regularized_zoning_experiments/visualize_regularized_results.py` | `plot_cluster_map`, `plot_cluster_map_vector`, called from `main` | `best_partition_map.{png,pdf}` (plus historical SVG) |
| same | same functions with `BLUE_MONO_COLORS` override | `best_partition_map_blue.{png,pdf}` (plus historical SVG) |

No other commit was used to infer or rewrite these figures.

## Historical inputs and style

The histogram reads `data/raw/beijing_orders_2017-06_2017-08.csv`, uses positive `finish_time - departure_time`, 120 bins over 0–120 min, a 6×4 inch canvas, gray `#bfbfbf` bars with black 0.5-point edges and alpha 0.9, serif/Times New Roman fallback fonts, density and frequency twin axes, dotted gray grid, PDF save DPI 600 and PNG save DPI 300. It has no legend.

The maps historically read the selected regularized partition, classified road edges, fifth-ring boundary and `road_poi_order` relation graph. They use a 1800×1800 canvas at 300 DPI, 0.45-point cluster lines, 2.5× halos, connector layer `#bdbdbd` at alpha 70/255, black 1.2-point boundary, adjacency-aware cluster ordering and colors, transparent background, equal aspect, and boundary-derived square extent. Layers are connectors (z=1), halo (z=2), cluster roads (z=3), boundary (z=4). The blue variant keeps the layout and substitutes `BLUE_MONO_COLORS` plus halo `#d8e2ee`. Neither map has a legend or text label.

## Recovery

- Reusable modules:
  - `src/roadnet_partition/reporting/best_partition_map.py`
  - `src/roadnet_partition/reporting/raw_order_trip_time_distribution.py`
- Entrypoints:
  - `scripts/figures/best_partition_maps.py`
  - `scripts/figures/raw_order_trip_time_distribution.py`
- Shared authoritative calculation: `roadnet_partition.downstream.tte.trip_time_minutes`.
- Map partition input: current Linux canonical `data/processed/fifth_ring/partition/canonical_partition.gpkg`; its cluster mapping and groups exactly match the Golden expected partition.
- No historical business CLI, `src/lib`, `sys.path` injection, selection logic or partition search was restored.

## Generated and verified

Generated under `reports/paper/figures/`:

- `best_partition_map.png` and `.pdf`
- `best_partition_map_blue.png` and `.pdf`
- `raw_order_trip_time_distribution.png` and `.pdf`
- `partition_and_mean_hourly_orders.png` and `.pdf` — corrected target

All PNG and PDF files open successfully. The corrected target PNG is 3740×1800 at 300 DPI. Its PDF keeps map geometry and text as vectors; the only embedded raster is the narrow 26×526 colorbar gradient. The restored histogram PNG is pixel-identical to the existing historical PNG. Input and output hashes are in `reports/paper/figure_manifest.json` and `reports/paper/checksums.sha256`.

No Partition, Demand, Supply or TTE pipeline stage was run, and no file under `data/processed` was written.

## Not recovered

Single-format historical outputs in the same regularized visualization script were intentionally not recovered: `baseline_vs_best_maps.png`, `best_connector_zoom.png`, parameter heatmaps, objective plots and metric comparison. Historical SVG companions were also skipped because the requested contract is PNG+PDF. Existing `tte_trip_time_distribution.{png,pdf}` was not found in the priority commit's dual-save code and remains unaudited. The earlier single-map and histogram recoveries are retained as audit by-products, not identified as the corrected target.
