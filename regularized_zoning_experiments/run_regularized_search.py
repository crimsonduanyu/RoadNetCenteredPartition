"""Experiment CLI for the regularized partition search.

The search/objective core now lives in ``lib.regularized`` (importable, testable).
This file is a thin entry point that loads a regularized-style config file and
delegates to ``lib.regularized.run_from_config``. The unified Stage 1 runner
(``src/stages/stage1_partition.py``) calls the same core with a config built
from ``config.yaml``.

Usage:
    python regularized_zoning_experiments/run_regularized_search.py [config.yaml]
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Re-export the full core so existing importers (e.g. tests) keep working.
from roadnet_partition.zoning.regularized import *  # noqa: E402,F401,F403
from roadnet_partition.zoning.partition import load_config, run_from_config, validate_config  # noqa: E402
from roadnet_partition.io.geospatial import project_path  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    config_path = project_path(argv[0]) if argv else Path(__file__).with_name("config_v1.yaml")
    config = load_config(config_path)
    validate_config(config)
    run_from_config(config, config_path)


if __name__ == "__main__":
    main()
