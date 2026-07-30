# Local data directory

This repository does not distribute the private Beijing payloads used by the
full study. Prepare local files according to [docs/data.md](../docs/data.md) and
the paths in `configs/datasets/` and `configs/pipelines/`.

`raw/`, `interim/`, and `processed/` are Git-ignored. Do not commit order rows,
driver identifiers, precise trip coordinates, or private derived matrices.
