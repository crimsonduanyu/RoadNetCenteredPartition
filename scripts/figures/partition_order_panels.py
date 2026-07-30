from pathlib import Path

from roadnet_partition.reporting.best_partition_map import render_partition_order_figure


PROJECT_ROOT = Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    output = PROJECT_ROOT / "artifacts/paper/figures/partition_and_mean_hourly_orders"
    render_partition_order_figure(
        PROJECT_ROOT / "data/processed/fifth_ring/partition/canonical_partition.gpkg",
        PROJECT_ROOT / "data/interim/fifth_ring/road_edges_classified.gpkg",
        PROJECT_ROOT / "data/raw/beijing_fifth_ring_boundary.gpkg",
        PROJECT_ROOT / "data/interim/fifth_ring/frozen_inputs/segment_relation_graph_road_poi_order.gpickle",
        PROJECT_ROOT / "data/interim/fifth_ring/frozen_inputs/segment_order_od_hourly.csv",
        output.with_suffix(".png"),
        output.with_suffix(".pdf"),
    )
