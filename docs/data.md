# Data and privacy

Private data is not distributed with this repository. Local payloads belong
under `data/raw/`, `data/interim/`, and `data/processed/`; these directories are
ignored except for documentation or explicitly reviewed synthetic samples.

## Input schemas

The authoritative column mapping is `configs/pipelines/demand.yaml`.

Raw orders require identifiers, service type, pickup/drop-off coordinates and
timestamps. The default names are:

```text
order_id, driver_id, service_type,
starting_lng, starting_lat, dest_lng, dest_lat,
departure_time, finish_time
```

Timestamps use `YYYY-MM-DD HH:MM:SS`. Coordinates use EPSG:4326. Users must
provide data they are legally permitted to process and update the configuration
when their column names differ.

POI input requires longitude, latitude and a category column. The defaults are
`大地X`, `大地Y`, and `类型1`. Road inputs and CRS declarations are defined in
`configs/datasets/*.yaml`; the Fifth Ring working CRS is EPSG:32650.

## Published stage products

- Partition: a GeoPackage and CSV containing `seg_id`, `cluster_id`, geometry,
  initialization and setting provenance.
- Demand: assigned orders, cluster index, OD table/tensor, POI features and
  graph products described by its `metadata.json`.
- Supply: `supply_inservice_od.csv.gz`, `supply_available_floor.csv.gz`,
  `supply_fleet_lower_bound.csv.gz`, `run_summary.json`, and `config_used.json`.
- TTE: `TTE_raw.parquet`, `TTE_count.parquet`, `TTE_imputed.parquet`,
  `TTE_hops.parquet`, `TTE_support.parquet`, network distance, and
  representative-node metadata.

The TTE matrices use a datetime index and ordered `origin->destination` cluster
columns. The default grid has ten-minute slots; observed trip times are positive
durations in the configured 3–80 minute range.

## Provenance assets

- `artifacts/golden/beijing-fifth-ring-v1/`: regression metadata; private
  payload is local-only.
- `artifacts/baselines/fifth-ring-windows-v1/`: historical Windows baseline
  metadata; its payload is private and local-only.
- `configs/policies/fifth_ring_linux_canonical_v1.yaml`: Linux canonical
  publication decision.

Neither a manifest nor a successful reproduction export grants a public data
license. Do not commit order rows, driver identifiers, precise trip coordinates,
or derived private matrices.
