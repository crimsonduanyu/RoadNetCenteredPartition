# Installation

Python 3.11 and 64-bit Linux are the supported reference platform. Create a
fresh `dydl` Conda environment:

```bash
conda env create --prefix ./.conda/dydl -f environment.yml
conda run --prefix ./.conda/dydl pip install -e . --no-deps
conda run --prefix ./.conda/dydl roadnet-partition --help
```

The package does not download private raw inputs or optional external Golden data.
