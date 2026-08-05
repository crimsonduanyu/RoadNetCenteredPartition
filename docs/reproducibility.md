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
  --output <version> \
  --profile minimal --dry-run
```

The exporter uses an explicit allowlist and blocks private, restricted, or
unknown full-data products. The destination must be a direct child of the
controlled external `<project-directory-name>-releases/` root next to the
project. Relative output names resolve under that root; project-internal and
unmarked roots are rejected before any directory or staging write. Generation
does not establish redistribution rights.

## Runtime and Git provenance

Run manifests record installed/unavailable status for NumPy, pandas, SciPy,
GeoPandas, Shapely, PyProj, OSMnx, NetworkX, PyArrow, DuckDB, python-igraph,
leidenalg, PyMetis, python-louvain, scikit-learn, Fiona, Pyogrio, Rtree,
Matplotlib, PyYAML, and tqdm. Native records cover GEOS, PROJ, Fiona/Pyogrio
GDAL sources, SQLite, DuckDB, igraph core, and an explicit unavailable BLAS
record when no stable path-free public API exists. Hostname, user, HOME,
environment variables, and absolute executable/library paths are not stored.

The Git record uses a binary `git diff HEAD` with external diff and textconv
disabled. Untracked files come only from Git's NUL-delimited
`ls-files --others --exclude-standard`; each regular file is streamed into a
size/mode/SHA-256 record, but its contents are never stored. Git-ignored
outputs, raw/environment directories, and local settings do not participate.
Git-visible untracked symlinks or special files are rejected rather than
followed. `--allow-dirty` permits a complete byte-addressed dirty record; it
does not weaken publish/export provenance validation.

Legacy manifests remain inspectable, but absent historical runtime or dirty
bytes cannot be backfilled from the current machine. They must be recomputed
before publish/export. This changes manifest/resume eligibility only; formal
artifact formats and canonical data contracts are unchanged.

Published scopes and reproduction bundles carry no executable serialization:
publish and export both refuse a `.gpickle`, `.pkl`, or `.pickle` file by name,
so a bundle consumer is never handed code to deserialize. The Preparation
relation graph is a schema-validated gzip+JSON artifact; see `docs/data.md`.

Long-lived assets are checksum-managed:

```bash
roadnet-partition validate --run outputs/runs/raw-only-reproduction
```

Golden and baseline payload checks only work when the corresponding private
local payload has been materialized.
