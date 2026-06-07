"""Stage 2 - demand dataset construction.

Reads the unified ``config.yaml`` (the ``order_pipeline`` section) and builds the
cluster-level demand dataset from the frozen canonical partition: assigns orders
to clusters, infers service types, exports assigned orders, and builds the OD
table/tensor plus the road/POI/distance cluster graphs.

The computation lives in ``lib.order_dataset``; this script only points it at the
project config. Run:  python src/stages/stage2_demand.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lib import order_dataset  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    config_arg = argv[0] if argv else str(CONFIG_PATH)
    order_dataset.main([config_arg])


if __name__ == "__main__":
    main()
