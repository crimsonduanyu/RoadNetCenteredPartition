# Development

Core code lives under `src/roadnet_partition/`; command-line orchestration is in
`roadnet_partition.cli`; analysis-only entrypoints live under `scripts/`.

Before committing:

```bash
conda run --prefix ./.conda/dydl pip install -e . --no-deps
conda run --prefix ./.conda/dydl python -m pytest
git diff --check
```

Use `--allow-dirty` only when the dirty bytes are intentional. The manifest
hashes the binary tracked diff and every Git-visible untracked regular file;
ignored files are excluded by Git, without a repository filesystem walk. Keep
the single dependency mapping and canonical collectors in `io/manifests.py` in
sync with their provenance tests.

Do not write canonical data from ordinary stage code. Runs own their directories,
validation is read-only with respect to algorithms and canonical products, and
only the publish transaction may replace `data/processed/<scope>/`.

Configuration is split between `configs/datasets/`, `configs/zoning/`, and
`configs/pipelines/`. Paths resolve relative to the file that declares them.
`configs/legacy/` is historical evidence and has no runtime reader.

Pull requests should state affected stages, commands run, data classification,
and whether outputs or figure checksums changed. Never include private raw data,
credentials, local agent settings, or generated `outputs/` content.
