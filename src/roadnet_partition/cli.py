from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from roadnet_partition import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roadnet-partition",
        description="Road-network-centered partitioning and dataset tools.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not args:
        parser.print_help()
        return 0
    parser.parse_args(args)
    return 0
