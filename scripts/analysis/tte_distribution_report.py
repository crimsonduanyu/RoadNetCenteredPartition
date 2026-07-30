"""Report raw order-level trip-time distribution and render a histogram.

Default input is the original Beijing order CSV:

    data/raw/beijing_orders_2017-06_2017-08.csv

The main statistics use valid positive order-level trip times:

    trip_time = finish_time - departure_time

No spatial matching, fifth-ring clipping, OD-slot aggregation, or imputation is
applied. The 3-80 minute Stage 4 filter is reported only as a comparison count.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import time
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "data" / "raw" / "beijing_orders_2017-06_2017-08.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "outputs" / "reports" / "raw-order-trip-time-distribution"
DEFAULT_REPORT = DEFAULT_OUTPUT / "report.md"
DEFAULT_TABLE = DEFAULT_OUTPUT / "summary.csv"
DEFAULT_PDF = DEFAULT_OUTPUT / "figure.pdf"
DEFAULT_PNG = DEFAULT_OUTPUT / "figure.png"

USECOLS = ["departure_time", "finish_time"]
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze raw order-level trip times and generate a distribution report."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Path to raw Beijing order CSV.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="Output Markdown report path.")
    parser.add_argument("--table", type=Path, default=DEFAULT_TABLE, help="Output summary CSV path.")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF, help="Output PDF figure path.")
    parser.add_argument("--png", type=Path, default=DEFAULT_PNG, help="Output PNG figure path.")
    parser.add_argument("--chunksize", type=int, default=2_000_000, help="CSV rows per chunk.")
    parser.add_argument("--bins", type=int, default=120, help="Histogram bin count.")
    parser.add_argument("--range-min", type=float, default=0.0, help="Histogram x-axis lower bound.")
    parser.add_argument("--range-max", type=float, default=120.0, help="Histogram x-axis upper bound.")
    return parser.parse_args()


def _concat(parts: Iterable[np.ndarray]) -> np.ndarray:
    arrays = [part for part in parts if part.size]
    if not arrays:
        return np.array([], dtype=np.float64)
    return np.concatenate(arrays).astype(np.float64, copy=False)


def load_raw_trip_times(path: Path, chunksize: int) -> tuple[np.ndarray, dict[str, int]]:
    """Read raw orders in chunks and return valid positive trip times in minutes."""
    trip_time_parts: list[np.ndarray] = []
    counters = {
        "raw_rows": 0,
        "valid_time_rows": 0,
        "invalid_time_rows": 0,
        "positive_trip_rows": 0,
        "nonpositive_trip_rows": 0,
        "stage_3_80_rows": 0,
        "below_3min_rows": 0,
        "above_80min_rows": 0,
        "above_120min_rows": 0,
    }

    reader = pd.read_csv(path, usecols=USECOLS, chunksize=chunksize)
    for i, chunk in enumerate(reader, start=1):
        counters["raw_rows"] += int(len(chunk))
        dep = pd.to_datetime(chunk["departure_time"], format=TIME_FMT, errors="coerce")
        fin = pd.to_datetime(chunk["finish_time"], format=TIME_FMT, errors="coerce")
        valid_time = dep.notna().to_numpy() & fin.notna().to_numpy()
        counters["valid_time_rows"] += int(valid_time.sum())
        counters["invalid_time_rows"] += int((~valid_time).sum())

        trip_time = (fin - dep).dt.total_seconds().to_numpy(dtype=np.float64) / 60.0
        valid_trip = valid_time & np.isfinite(trip_time)
        positive = valid_trip & (trip_time > 0)
        nonpositive = valid_trip & (trip_time <= 0)
        values = trip_time[positive]

        counters["positive_trip_rows"] += int(values.size)
        counters["nonpositive_trip_rows"] += int(nonpositive.sum())
        counters["stage_3_80_rows"] += int(((values >= 3.0) & (values <= 80.0)).sum())
        counters["below_3min_rows"] += int((values < 3.0).sum())
        counters["above_80min_rows"] += int((values > 80.0).sum())
        counters["above_120min_rows"] += int((values > 120.0).sum())
        trip_time_parts.append(values)

        log(
            f"chunk {i}: read {len(chunk):,}, positive trips {values.size:,} "
            f"(cum {counters['positive_trip_rows']:,})"
        )

    return _concat(trip_time_parts), counters


def summarize(values: np.ndarray, counters: dict[str, int]) -> dict[str, float | int]:
    if values.size == 0:
        raise ValueError("No valid positive trip times found.")
    quantiles = np.percentile(values, [1, 25, 50, 75, 90, 95, 99, 99.9])
    n = int(values.size)
    raw_rows = int(counters["raw_rows"])
    return {
        **counters,
        "positive_trip_ratio_raw": float(n / raw_rows) if raw_rows else float("nan"),
        "invalid_time_ratio_raw": float(counters["invalid_time_rows"] / raw_rows) if raw_rows else float("nan"),
        "nonpositive_trip_ratio_valid_time": (
            float(counters["nonpositive_trip_rows"] / counters["valid_time_rows"])
            if counters["valid_time_rows"]
            else float("nan")
        ),
        "stage_3_80_ratio_positive": float(counters["stage_3_80_rows"] / n),
        "below_3min_ratio_positive": float(counters["below_3min_rows"] / n),
        "above_80min_ratio_positive": float(counters["above_80min_rows"] / n),
        "above_120min_ratio_positive": float(counters["above_120min_rows"] / n),
        "min_min": float(np.min(values)),
        "p01_min": float(quantiles[0]),
        "p25_min": float(quantiles[1]),
        "median_min": float(quantiles[2]),
        "p75_min": float(quantiles[3]),
        "p90_min": float(quantiles[4]),
        "p95_min": float(quantiles[5]),
        "p99_min": float(quantiles[6]),
        "p999_min": float(quantiles[7]),
        "max_min": float(np.max(values)),
        "mean_min": float(np.mean(values)),
        "std_min": float(np.std(values)),
    }


def write_summary_table(summary: dict[str, float | int], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([summary]).to_csv(path, index=False, encoding="utf-8-sig")
    log(f"Wrote {path}")


def plot_histogram(
    values: np.ndarray,
    pdf_path: Path,
    png_path: Path,
    bins_num: int,
    range_lim: tuple[float, float],
) -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.parent.mkdir(parents=True, exist_ok=True)

    plt.rcParams["font.family"] = "serif"
    plt.rcParams["font.serif"] = ["Times New Roman", "DejaVu Serif", "serif"]
    plt.rcParams["font.size"] = 12

    fig, ax1 = plt.subplots(figsize=(6, 4))
    bin_width = (range_lim[1] - range_lim[0]) / bins_num

    ax1.hist(
        values,
        bins=bins_num,
        range=range_lim,
        density=True,
        color="#bfbfbf",
        edgecolor="black",
        linewidth=0.5,
        alpha=0.9,
    )
    ax1.set_xlabel("Trip Time (min)", fontsize=14)
    ax1.set_ylabel("Probability Density", fontsize=14)
    ax1.set_xlim(range_lim)
    ax1.set_ylim(0, 0.05)
    ax1.tick_params(direction="in", top=True, right=False, length=4)

    ax2 = ax1.twinx()
    count_max = ax1.get_ylim()[1] * len(values) * bin_width
    ax2.set_ylim(0, count_max)
    ax2.set_yticks(np.linspace(0, count_max, 6))
    ax2.set_ylabel("Frequency", fontsize=14)
    formatter = ticker.ScalarFormatter(useMathText=True)
    formatter.set_powerlimits((-2, 3))
    ax2.yaxis.set_major_formatter(formatter)
    ax2.tick_params(direction="in", left=False, length=4)

    ax1.grid(True, linestyle=":", alpha=0.4, color="gray")
    fig.tight_layout()
    fig.savefig(pdf_path, format="pdf", dpi=600)
    fig.savefig(png_path, format="png", dpi=300)
    plt.close(fig)
    log(f"Wrote {pdf_path}")
    log(f"Wrote {png_path}")


def pct(value: float | int) -> str:
    return f"{100.0 * float(value):.4f}%"


def write_report(
    summary: dict[str, float | int],
    input_path: Path,
    report_path: Path,
    table_path: Path,
    pdf_path: Path,
    png_path: Path,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    median = float(summary["median_min"])
    p99 = float(summary["p99_min"])

    lines = [
        "# Raw Order Trip-Time Distribution Report",
        "",
        "## Data",
        "",
        f"- Input: `{input_path.as_posix()}`",
        "- Scope: original order rows with parseable `departure_time` and `finish_time`, "
        "using positive order-level trip times only.",
        "- Formula: `trip_time = (finish_time - departure_time).total_seconds() / 60`.",
        "- Exclusions: no spatial clipping, no fifth-ring matching, no OD-slot aggregation, no imputation.",
        "",
        "## Data Quality",
        "",
        f"- Raw rows: {summary['raw_rows']:,}.",
        f"- Valid timestamp rows: {summary['valid_time_rows']:,}; invalid timestamp rows: "
        f"{summary['invalid_time_rows']:,} ({pct(summary['invalid_time_ratio_raw'])} of raw rows).",
        f"- Positive trip-time rows used for main statistics: {summary['positive_trip_rows']:,} "
        f"({pct(summary['positive_trip_ratio_raw'])} of raw rows).",
        f"- Non-positive trip-time rows excluded: {summary['nonpositive_trip_rows']:,} "
        f"({pct(summary['nonpositive_trip_ratio_valid_time'])} of valid timestamp rows).",
        f"- Trips inside the Stage 4 comparison band `[3, 80]` min: {summary['stage_3_80_rows']:,} "
        f"({pct(summary['stage_3_80_ratio_positive'])} of positive trips).",
        f"- Positive trips above 120 min: {summary['above_120min_rows']:,} "
        f"({pct(summary['above_120min_ratio_positive'])} of positive trips; outside the plotted range).",
        "",
        "## Key Statistics",
        "",
        f"- Median trip time: **{median:.2f} min**.",
        f"- 99th percentile trip time: **{p99:.2f} min**.",
        f"- Mean / std: {summary['mean_min']:.2f} / {summary['std_min']:.2f} min.",
        f"- P1 / P25 / P75: {summary['p01_min']:.2f} / {summary['p25_min']:.2f} / "
        f"{summary['p75_min']:.2f} min.",
        f"- P90 / P95 / P99.9: {summary['p90_min']:.2f} / {summary['p95_min']:.2f} / "
        f"{summary['p999_min']:.2f} min.",
        f"- Min / max among positive trips: {summary['min_min']:.2f} / {summary['max_min']:.2f} min.",
        "",
        "## Distribution Insight",
        "",
        f"The raw order-level trip-time distribution is right-skewed: the median is {median:.2f} min, "
        f"while the upper 1% starts at {p99:.2f} min. The old processed-matrix result is not "
        "comparable as a primary statistic because it was computed after spatial matching, clipping, "
        "and OD-slot median aggregation.",
        "",
        "## Artifacts",
        "",
        f"- Summary CSV: `{table_path.as_posix()}`",
        f"- Histogram PDF: `{pdf_path.as_posix()}`",
        f"- Histogram PNG: `{png_path.as_posix()}`",
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"Wrote {report_path}")


def main() -> None:
    args = parse_args()
    input_path = args.input.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Raw order input not found: {input_path}")

    log(f"Reading raw orders from {input_path}")
    values, counters = load_raw_trip_times(input_path, args.chunksize)
    log(f"Computing statistics for {values.size:,} positive trip times")
    summary = summarize(values, counters)

    table_path = args.table.resolve()
    pdf_path = args.pdf.resolve()
    png_path = args.png.resolve()
    report_path = args.report.resolve()

    write_summary_table(summary, table_path)
    plot_histogram(values, pdf_path, png_path, args.bins, (args.range_min, args.range_max))
    write_report(summary, input_path, report_path, table_path, pdf_path, png_path)

    print("\nRaw order trip-time distribution summary")
    print(f"  raw rows: {summary['raw_rows']:,}")
    print(f"  positive trip-time rows: {summary['positive_trip_rows']:,}")
    print(f"  median: {summary['median_min']:.2f} min")
    print(f"  p99: {summary['p99_min']:.2f} min")
    print(f"  histogram: {pdf_path}")


if __name__ == "__main__":
    main()
