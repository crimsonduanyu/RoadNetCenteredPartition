# Installation

Python 3.11 or newer is required. Repository acceptance is run in the existing
`dydl` Conda environment:

```bash
conda run -n dydl pip install -e . --no-deps
conda run -n dydl roadnet-partition --help
```

To create a standalone environment from the repository specification instead:

```bash
conda env create -f environment.yml
conda run -n bj_road_partition pip install -e . --no-deps
```

The package does not download Beijing orders, POI data, road-network extracts,
Golden payloads, or historical baselines.
