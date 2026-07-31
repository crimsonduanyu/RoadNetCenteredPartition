# Reproducibility

Install and run the public test suite:

```bash
conda run --prefix ./.conda/dydl pip install -e . --no-deps
conda run --prefix ./.conda/dydl python -m pytest
```

Tests construct synthetic fixtures in temporary directories and do not require
the private Beijing order payload. Full-data execution requires users to supply
their own inputs following [data.md](data.md).

Export a privacy-filtered reproduction package from a completed run:

```bash
conda run --prefix ./.conda/dydl roadnet-partition export-reproduction \
  --run outputs/runs/<run_id> \
  --output outputs/releases/reproduction/<version> \
  --profile minimal --dry-run
```

The exporter uses an explicit allowlist and blocks private, restricted, or
unknown full-data products. Generation does not establish redistribution rights.

Long-lived assets are checksum-managed:

```bash
roadnet-partition validate --run outputs/runs/raw-only-reproduction
```

Golden and baseline payload checks only work when the corresponding private
local payload has been materialized.
