# RoadNet-Centered Partition Pipeline

RoadNet-Centered Partition builds road-network-aware spatial clusters and the
downstream demand, supply, and trip-time-estimation datasets used by the study.

```text
partition → demand → supply → tte
```

Linux is the current Fifth Ring canonical platform. Private Beijing orders,
POI data, road extracts, Golden payloads, and the historical Windows payload are
not distributed through Git.

## Install

Acceptance uses the existing `dydl` Conda environment:

```bash
conda run -n dydl pip install -e . --no-deps
conda run -n dydl python -m pytest
```

See [installation](docs/installation.md) for environment creation.

## Quick run

The test suite is the public synthetic-data example. It creates temporary tiny
fixtures and does not require private data:

```bash
conda run -n dydl python -m pytest
```

Run the complete private-data pipeline after preparing inputs:

```bash
conda run -n dydl roadnet-partition run \
  --config configs/pipelines/full.yaml
```

Ordinary runs write only to `outputs/runs/<run_id>/`. Individual stages are
available as `roadnet-partition partition|demand|supply|tte`; use `--help` for
their lifecycle and configuration options.

## Validate, publish, and export

```bash
conda run -n dydl roadnet-partition validate \
  --run outputs/runs/<run_id> \
  --golden artifacts/golden/beijing-fifth-ring-v1

conda run -n dydl roadnet-partition publish \
  --run outputs/runs/<run_id> \
  --scope fifth_ring --overwrite \
  --baseline-decision configs/policies/fifth_ring_linux_canonical_v1.yaml \
  --dry-run

conda run -n dydl roadnet-partition export-reproduction \
  --run outputs/runs/<run_id> \
  --output outputs/releases/reproduction/<version> \
  --profile minimal --dry-run
```

Publishing never reruns algorithms. Reproduction export uses a privacy
allowlist and does not grant redistribution rights for upstream data.

## Prepare your own data

Input schemas, column mappings, CRS requirements, product contracts, provenance,
and privacy rules are documented in [data.md](docs/data.md). Do not commit order
rows, driver identifiers, precise trip coordinates, or private derived matrices.

## Publication figures

Tracked figures and their manifest live under `artifacts/paper/`:

```bash
conda run -n dydl python scripts/figures/best_partition_maps.py
conda run -n dydl python scripts/figures/partition_order_panels.py
conda run -n dydl python scripts/figures/raw_order_trip_time_distribution.py
sha256sum -c artifacts/paper/checksums.sha256
```

See [publication-figures.md](docs/publication-figures.md) for inputs and style.

## Repository layout

```text
artifacts/                  long-lived manifest-managed assets
  golden/                   regression metadata; payload local-only
  baselines/                historical metadata; payload local-only
  paper/                    tracked publication figures and checksums
configs/                    dataset, stage, zoning, and policy configuration
data/                       local private/canonical data; payload ignored
docs/                       public documentation and condensed history
outputs/                    generated runs, validation, reports, releases; ignored
scripts/                    analysis and publication-figure entrypoints
src/roadnet_partition/      importable package and CLI
tests/                      synthetic/unit/integration tests
```

## Documentation

- [Pipeline](docs/pipeline.md)
- [Reproducibility](docs/reproducibility.md)
- [Development](docs/development.md)
- [Refactor history](docs/history/refactor-v1.md)

## License and citation

Code is released under the [MIT License](LICENSE). Cite the project using
[`CITATION.cff`](CITATION.cff). Data licenses are separate and must be obtained
from the corresponding data providers.
