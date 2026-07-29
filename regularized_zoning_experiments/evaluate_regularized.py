"""Compatibility CLI for regularized partition evaluation."""

from pathlib import Path
import sys

from roadnet_partition.zoning.evaluate import *  # noqa: F401,F403
from roadnet_partition.zoning.evaluate import project_path, run_regularized_evaluation

load_config = load_evaluation_config


def main(argv: list[str] | None = None) -> None:
    argv = argv or sys.argv[1:]
    config_path = project_path(argv[0]) if argv else Path(__file__).with_name("config_v1.yaml")
    run_regularized_evaluation(config_path)


if __name__ == "__main__":
    main()
