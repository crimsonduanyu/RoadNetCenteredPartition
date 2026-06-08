"""Run the full three-stage pipeline in order.

Each stage is a standalone script that reads the unified ``config.yaml`` and runs
in its own process (stages are memory-heavy, so isolation matters):

    Stage 1  spatial partitioning   (regularized search; reproduces canonical)
    Stage 2  demand dataset         (consumes the frozen canonical partition)
    Stage 3  supply-state           (consumes the Stage 2 output)
    Stage 4  trip-time (TTE) dataset (consumes the Stage 2 output)

Stage 1 runs with ``--verify`` so a reproduction mismatch against the frozen
canonical partition fails the whole run loudly.

Run the whole pipeline:  python src/run_pipeline.py
Run a single stage:      python src/stages/stage2_demand.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STAGES: list[tuple[str, str, list[str]]] = [
    ("Stage 1: spatial partitioning", "stages/stage1_partition.py", ["--verify"]),
    ("Stage 2: demand dataset", "stages/stage2_demand.py", []),
    ("Stage 3: supply-state reconstruction", "stages/stage3_supply.py", []),
    ("Stage 4: trip-time (TTE) dataset", "stages/stage4_tte.py", []),
]


def main() -> None:
    for index, (label, stage, args) in enumerate(STAGES, start=1):
        print(f"\n=== [{index}/{len(STAGES)}] {label} ({stage}) ===", flush=True)
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "src" / stage), *args],
            check=True,
        )

    print("\nPipeline completed successfully.")


if __name__ == "__main__":
    main()
