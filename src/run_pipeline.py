from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGES = [
    "00_download_osm.py",
    "01_preprocess_roads.py",
    "02_build_poi_features.py",
    "02_build_order_features.py",
    "02_build_segment_relation_graph.py",
    "03_cluster_segments.py",
    "04_visualize_clusters.py",
]


def main() -> None:
    for index, stage in enumerate(STAGES, start=1):
        print(f"\n=== [{index}/{len(STAGES)}] Running {stage} ===", flush=True)
        subprocess.run([sys.executable, str(PROJECT_ROOT / "src" / stage)], check=True)

    print("\nPipeline completed successfully.")
    print(f"Outputs are available under {PROJECT_ROOT / 'data'} and {PROJECT_ROOT / 'outputs'}")


if __name__ == "__main__":
    main()
