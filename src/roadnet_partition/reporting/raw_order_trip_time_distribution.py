"""Restore the paper-quality raw order trip-time histogram from commit d2139c6."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
import pandas as pd

from roadnet_partition.downstream.tte import DEPARTURE_COL, FINISH_COL, trip_time_minutes


def load_positive_trip_times(path: Path, chunksize: int = 2_000_000) -> np.ndarray:
    """Read valid positive order-level trip times without running the TTE stage."""
    parts: list[np.ndarray] = []
    for chunk in pd.read_csv(path, usecols=[DEPARTURE_COL, FINISH_COL], chunksize=chunksize):
        values = trip_time_minutes(chunk).to_numpy(dtype=np.float64)
        parts.append(values[np.isfinite(values) & (values > 0)])
    return _concat(parts)


def _concat(parts: Iterable[np.ndarray]) -> np.ndarray:
    arrays = [part for part in parts if part.size]
    if not arrays:
        raise ValueError("No valid positive trip times found.")
    return np.concatenate(arrays).astype(np.float64, copy=False)


def plot_histogram(
    values: np.ndarray,
    pdf_path: Path,
    png_path: Path,
    bins_num: int = 120,
    range_lim: tuple[float, float] = (0.0, 120.0),
) -> None:
    """Render the historical 6x4 inch histogram and save vector PDF plus 300-DPI PNG."""
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
