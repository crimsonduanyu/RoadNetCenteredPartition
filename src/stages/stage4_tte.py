"""Stage 4 - trip-time (TTE) dataset construction.

Reads the unified ``config.yaml`` (the ``stage4_tte`` section) and builds the
cluster-level trip-time matrix from the Stage 2 demand products: the assigned
orders (``orders_region_assigned.csv.gz``) and the cluster centroids
(``cluster_index.csv``). It produces the observed ``TTE_raw.parquet`` and the
transitively imputed ``TTE_imputed.parquet`` under ``data/processed/<scope>/tte/``.

Stage 4 depends only on Stage 2 outputs (not Stage 3). The computation lives in
``roadnet_partition.downstream.tte``; this script only points it at the project
config. Run:

    python src/stages/stage4_tte.py
    python src/stages/stage4_tte.py path/to/config.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

from roadnet_partition.downstream import tte

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    config_arg = argv[0] if argv else str(CONFIG_PATH)
    tte.main([config_arg])


if __name__ == "__main__":
    main()
