"""Stage 3 - supply-state reconstruction.

Reads the unified ``config.yaml`` (the ``stage3_supply`` section) and runs the
driver-chain / idle-window / fleet-lower-bound reconstruction over the Stage 2
output (``orders_region_assigned.csv.gz``). The Fix-1 (natural-day slot clipping)
and Fix-2 (origin-only in-service attribution) corrections live in the migrated
``roadnet_partition.downstream.supply`` module.

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
import yaml

from roadnet_partition.downstream import supply
from roadnet_partition.io.paths import resolve_path

CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def load_unified_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def build_parser(cfg: dict) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 3: supply-state reconstruction (defaults from config.yaml).")
    parser.add_argument("--orders-path", default=cfg["orders_path"])
    parser.add_argument("--output-dir", default=cfg["output_dir"])
    parser.add_argument("--max-gap", type=int, default=int(cfg["max_gap_minutes"]))
    parser.add_argument("--tau-idle", type=int, default=int(cfg.get("tau_idle_minutes", 30)),
                        help="Idle-judgement cap in minutes (distinct from --max-gap chain formation).")
    parser.add_argument("--carpool-merge-gap-s", type=int, default=int(cfg["carpool_merge_gap_s"]))
    parser.add_argument("--slot-duration", type=int, default=int(cfg["slot_duration_min"]))
    parser.add_argument("--n-blocks", type=int, default=int(cfg.get("n_blocks", 8)),
                        help="Number of per-driver chunks for the chunked reconstruction.")
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
        orders_path=resolve_path(args.orders_path, base_dir=PROJECT_ROOT),
        output_dir=resolve_path(args.output_dir, base_dir=PROJECT_ROOT),
        max_gap_minutes=args.max_gap,
        tau_idle_minutes=args.tau_idle,
        carpool_merge_gap_s=args.carpool_merge_gap_s,
        slot_duration_min=args.slot_duration,
        n_blocks=args.n_blocks,
    )
    print("Supply reconstruction summary:")
    for key, value in summary.items():
        if key != "block_summaries":
            print(f"  {key}: {value}")
    return summary


if __name__ == "__main__":
    main()
