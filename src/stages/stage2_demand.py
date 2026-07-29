"""Compatibility entrypoint for Stage 2 Demand construction."""

from pathlib import Path
import sys

from roadnet_partition.downstream import demand


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    config_arg = argv[0] if argv else str(CONFIG_PATH)
    demand.main([config_arg])


if __name__ == "__main__":
    main()
