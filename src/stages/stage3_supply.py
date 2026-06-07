"""Stage 3 - supply-state reconstruction.

Reads the unified ``config.yaml`` (the ``stage3_supply`` section) and runs the
driver-chain / idle-window / fleet-lower-bound reconstruction over the Stage 2
output (``orders_region_assigned.csv.gz``). The Fix-1 (natural-day slot clipping)
and Fix-2 (origin-only in-service attribution) corrections live in ``lib.supply``.

Defaults come from config; CLI flags override them for partial/diagnostic runs:

    python src/stages/stage3_supply.py
    python src/stages/stage3_supply.py --start-date 2017-06-01 --end-date 2017-06-07
    python src/stages/stage3_supply.py --sample-days 3 --keep-daily-parts
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import yaml  # noqa: E402

from lib import supply  # noqa: E402
from lib.geo import project_path  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def load_unified_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def build_parser(cfg: dict) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 3: supply-state reconstruction (defaults from config.yaml).")
    parser.add_argument("--orders-path", default=cfg["orders_path"])
    parser.add_argument("--output-dir", default=cfg["output_dir"])
    parser.add_argument("--max-gap", type=int, default=int(cfg["max_gap_minutes"]))
    parser.add_argument("--carpool-merge-gap-s", type=int, default=int(cfg["carpool_merge_gap_s"]))
    parser.add_argument("--slot-duration", type=int, default=int(cfg["slot_duration_min"]))
    parser.add_argument("--io-chunk-rows", type=int, default=int(cfg["io_chunk_rows"]))
    parser.add_argument("--execution-mode", choices=["daily", "full-memory"], default="daily")
    parser.add_argument("--start-date", default=None, help="First departure date, inclusive (YYYY-MM-DD).")
    parser.add_argument("--end-date", default=None, help="Last departure date, inclusive (YYYY-MM-DD).")
    parser.add_argument("--sample-days", type=int, default=None, help="Process only the first N departure dates.")
    parser.add_argument("--keep-daily-parts", action="store_true", help="Keep _daily_parts after merging.")
    return parser


def main(argv: list[str] | None = None) -> dict:
    cfg = load_unified_config()["stage3_supply"]
    args = build_parser(cfg).parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    summary = supply.run_pipeline(
        orders_path=project_path(args.orders_path),
        output_dir=project_path(args.output_dir),
        max_gap_minutes=args.max_gap,
        carpool_merge_gap_s=args.carpool_merge_gap_s,
        slot_duration_min=args.slot_duration,
        io_chunk_rows=args.io_chunk_rows,
        execution_mode=args.execution_mode,
        start_date=args.start_date,
        end_date=args.end_date,
        sample_days=args.sample_days,
        keep_daily_parts=args.keep_daily_parts,
    )
    print("Supply reconstruction summary:")
    for key, value in summary.items():
        if key != "daily_summaries":
            print(f"  {key}: {value}")
    return summary


if __name__ == "__main__":
    main()
