"""Stage 3 - supply-state reconstruction.

Reads the unified ``config.yaml`` (the ``stage3_supply`` section) and runs the
driver-chain / idle-window / fleet-lower-bound reconstruction over the Stage 2
output (``orders_region_assigned.csv.gz``). The Fix-1 (natural-day slot clipping)
and Fix-2 (origin-only in-service attribution) corrections live in ``lib.supply``.

Run:  python src/stages/stage3_supply.py
"""
from __future__ import annotations

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


def main(argv: list[str] | None = None) -> None:
    config = load_unified_config()
    cfg = config["stage3_supply"]
    summary = supply.run_pipeline(
        orders_path=project_path(cfg["orders_path"]),
        output_dir=project_path(cfg["output_dir"]),
        max_gap_minutes=int(cfg["max_gap_minutes"]),
        carpool_merge_gap_s=int(cfg["carpool_merge_gap_s"]),
        slot_duration_min=int(cfg["slot_duration_min"]),
        io_chunk_rows=int(cfg["io_chunk_rows"]),
    )
    print("Supply reconstruction summary:")
    for key, value in summary.items():
        if key != "daily_summaries":
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
