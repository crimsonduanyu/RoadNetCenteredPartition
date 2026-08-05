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
from roadnet_partition.io.safe_graph import SafeGraphArtifactError
from roadnet_partition.io.serialization_policy import ExecutableSerializationRefused
from roadnet_partition.pipeline.publishing import PublishError
from roadnet_partition.pipeline.validation import ValidationError
from roadnet_partition.pipeline.stages import (
    ResumeConflictError,
    RunConflictError,
    StageContractError,
    execute_stage,
)
from roadnet_partition.releases.reproduction import ExportError


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
    parser.add_argument("--allow-dirty", action="store_true", help="Bind tracked and untracked Git bytes into this run identity.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roadnet-partition",
        description="Road-network-centered partitioning and dataset tools.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{check-raw,run,validate,publish,export-reproduction,partition,demand,supply,tte}",
    )
    check_parser = subparsers.add_parser("check-raw", help="Check required raw files and schemas without generating outputs.")
    check_parser.add_argument("--config", type=Path, required=True, help="Full pipeline YAML configuration.")
    pipeline_parser = subparsers.add_parser("run", help="Run the fixed partition → demand → supply → tte pipeline.")
    pipeline_parser.add_argument("--config", type=Path, required=True, help="Full pipeline YAML configuration.")
    pipeline_parser.add_argument("--run-id")
    pipeline_parser.add_argument("--run-dir", type=Path)
    pipeline_parser.add_argument("--from-stage", choices=tuple(RESOLVERS))
    pipeline_parser.add_argument("--to-stage", choices=tuple(RESOLVERS), default="tte")
    pipeline_mode = pipeline_parser.add_mutually_exclusive_group()
    pipeline_mode.add_argument("--resume", action="store_true")
    pipeline_mode.add_argument("--overwrite", action="store_true")
    pipeline_parser.add_argument("--allow-dirty", action="store_true", help="Bind tracked and untracked Git bytes into this run identity.")
    pipeline_parser.add_argument(
        "--isolate-stages", action=argparse.BooleanOptionalAction, default=None,
        help="Run each stage in an internal child process (default comes from pipeline config).",
    )
    validate_parser = subparsers.add_parser("validate", help="Validate a completed pipeline run.")
    validate_parser.add_argument("--run", type=Path, required=True)
    validate_parser.add_argument("--golden", type=Path)
    validate_parser.add_argument("--report", type=Path)
    publish_parser = subparsers.add_parser("publish", help="Publish a validated run as one processed scope.")
    publish_parser.add_argument("--run", type=Path, required=True)
    publish_parser.add_argument(
        "--scope",
        required=True,
        help="One safe dataset identifier, not a path; it must match the run manifest scope.",
    )
    publish_parser.add_argument("--overwrite", action="store_true")
    publish_parser.add_argument("--allow-dirty", action="store_true")
    publish_parser.add_argument("--dry-run", action="store_true")
    publish_parser.add_argument("--baseline-decision", type=Path)
    export_parser = subparsers.add_parser("export-reproduction", help="Export a fixed-profile reproduction package.")
    export_parser.add_argument("--run", type=Path, required=True)
    export_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help=(
            "One release directory directly under the controlled external "
            "<project-name>-releases root; relative names resolve under that root."
        ),
    )
    export_parser.add_argument("--overwrite", action="store_true")
    export_parser.add_argument("--allow-dirty", action="store_true")
    export_parser.add_argument("--profile", choices=("minimal", "full"), default="minimal")
    export_parser.add_argument("--dry-run", action="store_true")
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
        if parsed.command == "check-raw":
            from roadnet_partition.pipeline.preparation import check_raw
            from roadnet_partition.pipeline.runner import resolve_pipeline_config

            pipeline = resolve_pipeline_config(parsed.config)
            preparation = pipeline.values.get("preparation")
            if not preparation:
                raise ConfigError(f"{parsed.config}: preparation.config is required for raw checks")
            check_raw(Path(preparation["config"]), pipeline.project_root)
            from roadnet_partition.pipeline.runner import _external_inputs
            records = _external_inputs(pipeline)
            for name, record in records.items():
                print(f"{name}: {record['size']} bytes {record['sha256']}")
            return 0
        if parsed.command == "validate":
            from roadnet_partition.pipeline.validation import validate_run

            report = validate_run(parsed.run, golden=parsed.golden, report=parsed.report)
            print(f"validation: {report['overall_status']}")
            return 0 if report["overall_status"] == "passed" else 5
        if parsed.command == "publish":
            from roadnet_partition.pipeline.publishing import publish_scope

            result = publish_scope(
                parsed.run, scope=parsed.scope, overwrite=parsed.overwrite,
                allow_dirty=parsed.allow_dirty, dry_run=parsed.dry_run,
                baseline_decision=parsed.baseline_decision,
            )
            if parsed.allow_dirty and (
                result["git"]["source"].get("dirty") is True
                or result["git"]["current"].get("dirty") is True
            ):
                print("warning: publishing from dirty Git state", file=sys.stderr)
            print(f"publish: {result['status']} -> {result['target']}")
            if parsed.dry_run:
                print(f"inventory: {result['file_count']} files, {result['total_size']} bytes")
            return 0
        if parsed.command == "export-reproduction":
            from roadnet_partition.releases.reproduction import export_reproduction

            result = export_reproduction(
                parsed.run, output=parsed.output, overwrite=parsed.overwrite,
                allow_dirty=parsed.allow_dirty, profile=parsed.profile, dry_run=parsed.dry_run,
            )
            if parsed.allow_dirty and (
                result["git"]["source"].get("dirty") is True
                or result["git"]["current"].get("dirty") is True
            ):
                print("warning: exporting from dirty Git state", file=sys.stderr)
            print(f"export-reproduction: {result['status']} -> {result['output']}")
            if parsed.dry_run:
                for item in result["inventory"]:
                    print(
                        f"candidate: {item['classification']} {item['release_path']} "
                        f"({item['size']} bytes)"
                    )
            if result.get("blocked_classifications"):
                print(f"blocked classifications: {result['blocked_classifications']}", file=sys.stderr)
                print(result["blocking_reason"], file=sys.stderr)
            return 0
        if parsed.command == "run":
            from roadnet_partition.pipeline.runner import resolve_pipeline_config, run_pipeline

            if parsed.overwrite and parsed.from_stage is None:
                parser.error("run --overwrite requires --from-stage")
            result = run_pipeline(
                resolve_pipeline_config(parsed.config),
                run_id=parsed.run_id,
                run_dir=parsed.run_dir,
                from_stage=parsed.from_stage or "partition",
                to_stage=parsed.to_stage,
                resume=parsed.resume,
                overwrite=parsed.overwrite,
                isolate_stages=parsed.isolate_stages,
                allow_dirty=parsed.allow_dirty,
            )
            print(
                f"pipeline: complete through {result.completed_through}; "
                f"all_required_stages_complete={str(result.all_required_stages_complete).lower()}"
            )
            return 0
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
            allow_dirty=parsed.allow_dirty,
        )
    except (ConfigError, UnsafePathError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except (SafeGraphArtifactError, ExecutableSerializationRefused) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    except StageContractError as error:
        print(str(error), file=sys.stderr)
        return 3
    except (ResumeConflictError, RunConflictError) as error:
        print(str(error), file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("pipeline interrupted", file=sys.stderr)
        return 130
    except (ValidationError, PublishError, ExportError) as error:
        print(str(error), file=sys.stderr)
        return 6
    except FileExistsError as error:
        print(str(error), file=sys.stderr)
        return 6
    reused = bool(result.metrics.get("resume_reused", False))
    print(f"{result.stage}: {'reused' if reused else result.status.value}")
    return 0
