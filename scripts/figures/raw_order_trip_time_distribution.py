from __future__ import annotations

import argparse
from pathlib import Path

from roadnet_partition.reporting.raw_order_trip_time_distribution import (
    load_positive_trip_times,
    plot_histogram,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore the raw order trip-time paper figure.")
    parser.add_argument(
        "--input",
        type=Path,
        default=PROJECT_ROOT / "data/raw/beijing_orders_2017-06_2017-08.csv",
    )
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "outputs/figures")
    parser.add_argument("--chunksize", type=int, default=2_000_000)
    args = parser.parse_args()

    values = load_positive_trip_times(args.input.resolve(), args.chunksize)
    stem = args.output_dir.resolve() / "raw_order_trip_time_distribution"
    plot_histogram(values, stem.with_suffix(".pdf"), stem.with_suffix(".png"))


if __name__ == "__main__":
    main()
