from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from roadnet_partition import __version__
from roadnet_partition.config import (
    ConfigError,
    resolve_demand_config,
    resolve_partition_config,
    resolve_supply_config,
    resolve_tte_config,
)
from roadnet_partition.io.paths import UnsafePathError
from roadnet_partition.pipeline.stages import (
    ResumeConflictError,
    RunConflictError,
    StageContractError,
    execute_stage,
)


RESOLVERS = {
    "partition": resolve_partition_config,
    "demand": resolve_demand_config,
    "supply": resolve_supply_config,
    "tte": resolve_tte_config,
}


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True, help="Split stage YAML configuration.")
    parser.add_argument("--run-id", help="Run identifier (default: timestamp/scope/stage/config hash).")
    parser.add_argument("--run-dir", type=Path, help="Owned run directory (default: <project_root>/outputs/runs/<run-id>).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="Reuse an exactly matching complete stage, or continue a failed/interrupted stage.")
    mode.add_argument("--overwrite", action="store_true", help="Replace only this owned stage; the run root is retained and manifest backed up.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roadnet-partition",
        description="Road-network-centered partitioning and dataset tools.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="{partition,demand,supply,tte}")
    for name in ("partition", "demand", "supply", "tte"):
        stage_parser = subparsers.add_parser(name, help=f"Run the {name} stage only.")
        _add_run_options(stage_parser)
        if name == "supply":
            stage_parser.add_argument(
                "--n-blocks",
                type=int,
                help="Override stage3_supply.n_blocks (default comes from the Supply config).",
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not args:
        parser.print_help()
        return 0
    parsed = parser.parse_args(args)
    if parsed.command is None:
        parser.print_help()
        return 0
    try:
        config = RESOLVERS[parsed.command](parsed.config)
        overrides = None
        if parsed.command == "supply" and parsed.n_blocks is not None:
            overrides = {"n_blocks": parsed.n_blocks}
        result = execute_stage(
            stage=parsed.command,
            config=config,
            run_id=parsed.run_id,
            run_dir=parsed.run_dir,
            resume=parsed.resume,
            overwrite=parsed.overwrite,
            overrides=overrides,
        )
    except (ConfigError, UnsafePathError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except StageContractError as error:
        print(str(error), file=sys.stderr)
        return 3
    except (ResumeConflictError, RunConflictError) as error:
        print(str(error), file=sys.stderr)
        return 4
    reused = bool(result.metrics.get("resume_reused", False))
    print(f"{result.stage}: {'reused' if reused else result.status.value}")
    return 0
