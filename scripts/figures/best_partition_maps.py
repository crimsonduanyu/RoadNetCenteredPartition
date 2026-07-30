import argparse
from pathlib import Path

from roadnet_partition.reporting.best_partition_map import render_partition_maps


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    argparse.ArgumentParser(description="Render the canonical partition paper maps.").parse_args()
    render_partition_maps(
        PROJECT_ROOT / "data/processed/fifth_ring/partition/canonical_partition.gpkg",
        PROJECT_ROOT / "data/interim/fifth_ring/road_edges_classified.gpkg",
        PROJECT_ROOT / "data/raw/beijing_fifth_ring_boundary.gpkg",
        PROJECT_ROOT / "data/interim/fifth_ring/frozen_inputs/segment_relation_graph_road_poi_order.gpickle",
        PROJECT_ROOT / "artifacts/paper/figures",
    )


if __name__ == "__main__":
    main()
