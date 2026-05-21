from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from utils_geo import get_active_scope, load_config


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGES = [
    # "00_download_osm.py",
    "01_preprocess_roads.py",
    "02_build_poi_features.py",
    "02_build_order_features.py",
    "02_build_segment_relation_graph.py",
    "03_cluster_segments.py",
    "04_visualize_clusters.py",
]


def main() -> None:
    config = load_config()
    scope = get_active_scope(config)
    print(f"Active study area: {scope['name']} ({scope['label']})", flush=True)

    for index, stage in enumerate(STAGES, start=1):
        print(f"\n=== [{index}/{len(STAGES)}] Running {stage} ===", flush=True)
        subprocess.run([sys.executable, str(PROJECT_ROOT / "src" / stage)], check=True)

    print("\nPipeline completed successfully.")
    print(f"Scope outputs are available under {PROJECT_ROOT / 'data'} and {PROJECT_ROOT / 'outputs' / scope['name']}")


if __name__ == "__main__":
    main()
