# RoadNet-Centered Partition Pipeline

RoadNet-Centered Partition builds road-network-aware zones and the downstream
demand, supply, and trip-time-estimation (TTE) datasets used by the study:

```text
raw data → preparation → Partition → Demand → Supply → TTE
```

The supported reference platform is 64-bit Linux with Conda, Python 3.11, and
at least 16 GiB RAM. A full Fifth Ring run takes about 1 hour 55 minutes, peaks
near 12 GiB RSS, and writes roughly 4–5 GiB under `outputs/runs/`.

## Install

Create a new `dydl` environment and install this checkout. Do not reuse an
environment when testing clean-room reproducibility.

```bash
conda env create --prefix ./.conda/dydl -f environment.yml
conda run --prefix ./.conda/dydl pip install -e . --no-deps
```

## Raw data

Private Beijing data is not distributed with this repository. Obtain files
from sources you are legally permitted to use and place them under `data/raw/`.
The Fifth Ring configuration requires:

| File | Format and role |
| --- | --- |
| `beijing_edges_raw.gpkg` | GeoPackage, EPSG:4326 OSM-style road edges with `u`, `v`, `highway`, and geometry |
| `beijing_fifth_ring_boundary.gpkg` | GeoPackage containing one Fifth Ring boundary polygon |
| `beijing_fifth_ring_segments.gpkg` | GeoPackage containing Fifth Ring road linework |
| `beijing_drive_within_fifth_ring.graphml` | OSMnx-compatible drive network used for TTE distance |
| `beijing_poi_2017.csv` | CSV with `大地X`, `大地Y`, and `类型1` |
| `beijing_order_201710.csv` | CSV used only to construct zoning demand features |
| `beijing_orders_2017-06_2017-08.csv` | CSV used by Demand, Supply, and TTE |

Order CSVs use EPSG:4326 coordinates and the columns `order_id`, `driver_id`,
`service_type`, `starting_lng`, `starting_lat`, `dest_lng`, `dest_lat`,
`departure_time`, and `finish_time`; timestamps use `YYYY-MM-DD HH:MM:SS`.
See [docs/data.md](docs/data.md) for schemas, provenance, and privacy rules.

Check file presence, hashes, CRS metadata, and preparation CSV columns without
creating generated data:

```bash
conda run --prefix ./.conda/dydl roadnet-partition check-raw --config configs/pipelines/full.yaml
```

## Complete run

This single command creates all preparation assets inside the run and then
executes the four formal stages. It does not read `artifacts/`, frozen inputs,
published canonical data, or older runs.

```bash
conda run --prefix ./.conda/dydl roadnet-partition run \
  --config configs/pipelines/full.yaml \
  --run-id raw-only-reproduction
```

Generated files are owned by `outputs/runs/raw-only-reproduction/`; no normal
run writes `data/interim/` or `data/processed/`. After an interruption, resume
the exact run with:

```bash
conda run --prefix ./.conda/dydl roadnet-partition run \
  --config configs/pipelines/full.yaml \
  --run-id raw-only-reproduction \
  --resume
```

Validate the completed run without a Golden payload:

```bash
conda run --prefix ./.conda/dydl roadnet-partition validate \
  --run outputs/runs/raw-only-reproduction
```

Maintainers may additionally pass `--golden /absolute/or/relative/external/path`.
Golden validation is optional and no Golden data is stored in this repository.
`roadnet-partition publish` is also a maintainer-only operation; publishing a
canonical scope is not part of an ordinary user run.

Maintainers can inspect a privacy-filtered export without writing it:

```bash
conda run --prefix ./.conda/dydl roadnet-partition export-reproduction \
  --run outputs/runs/raw-only-reproduction \
  --output outputs/releases/reproduction/raw-only \
  --profile minimal --dry-run
```

## Publication figures

Generate PNG and PDF figures directly from the new run. Outputs default to
`outputs/figures/` and remain reproducible/ignored runtime products.

```bash
conda run --prefix ./.conda/dydl python scripts/figures/best_partition_maps.py \
  --run outputs/runs/raw-only-reproduction
conda run --prefix ./.conda/dydl python scripts/figures/raw_order_trip_time_distribution.py
```

The first command creates the partition maps and the two-panel partition/order
figure; the second creates the raw trip-time distribution. See
[docs/publication-figures.md](docs/publication-figures.md).

## Common errors

- `preparation input ... is missing`: place the named source file in
  `data/raw/`; never substitute a derived or previously generated file.
- `missing columns`: update your source-column mapping in the preparation or
  Demand config; do not rename private source data in place without recording
  provenance.
- process killed or out of memory: use a machine with at least 16 GiB RAM and
  close other memory-heavy jobs before resuming.
- run directory already exists: use the same `--run-id --resume`, or choose a
  new run ID; do not copy files between runs.
- Golden path missing: omit `--golden` for normal validation.

## Development and project layout

```text
configs/                    raw preparation and four-stage configuration
data/raw/                   local private raw inputs; Git ignored
docs/                       public installation, data, and pipeline guidance
outputs/runs/               generated run-owned products; Git ignored
outputs/figures/            generated paper figures; Git ignored
scripts/figures/            figure entrypoints
src/roadnet_partition/      package and CLI
tests/                      synthetic/unit/integration tests
```

Run public tests with `conda run --prefix ./.conda/dydl python -m pytest`. Development details
are in [docs/development.md](docs/development.md), and the full pipeline contract
is in [docs/pipeline.md](docs/pipeline.md).

Code is released under the [MIT License](LICENSE). Cite the project using
[`CITATION.cff`](CITATION.cff); upstream data licenses remain separate.
