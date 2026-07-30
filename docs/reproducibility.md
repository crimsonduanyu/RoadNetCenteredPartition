# Reproducibility

Install and run the public test suite:

```bash
conda run -n dydl pip install -e . --no-deps
conda run -n dydl python -m pytest
```

Tests construct synthetic fixtures in temporary directories and do not require
the private Beijing order payload. Full-data execution requires users to supply
their own inputs following [data.md](data.md).

Export a privacy-filtered reproduction package from a completed run:

```bash
conda run -n dydl roadnet-partition export-reproduction \
  --run outputs/runs/<run_id> \
  --output outputs/releases/reproduction/<version> \
  --profile minimal --dry-run
```

The exporter uses an explicit allowlist and blocks private, restricted, or
unknown full-data products. Generation does not establish redistribution rights.

Long-lived assets are checksum-managed:

```bash
sha256sum -c artifacts/paper/checksums.sha256
(cd artifacts/golden/beijing-fifth-ring-v1 && sha256sum -c checksums.sha256)
(cd artifacts/baselines/fifth-ring-windows-v1 && sha256sum -c checksums.sha256)
```

Golden and baseline payload checks only work when the corresponding private
local payload has been materialized.
