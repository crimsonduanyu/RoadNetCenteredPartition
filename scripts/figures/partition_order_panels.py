import argparse
import json
from pathlib import Path

from roadnet_partition.pipeline.stages import canonical_partition_output_key
from roadnet_partition.reporting.best_partition_map import render_partition_order_figure


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the two-panel partition/order figure from one run.")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "outputs/figures")
    args = parser.parse_args()
    run = args.run.resolve()
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    outputs = manifest["stages"]["partition"]["outputs"]
    partition = Path(outputs[canonical_partition_output_key(outputs)]["path"])
    preparation = run / "preparation"
    output = args.output_dir.resolve() / "partition_and_mean_hourly_orders"
    render_partition_order_figure(
        partition,
        preparation / "road_edges_classified.gpkg",
        PROJECT_ROOT / "data/raw/beijing_fifth_ring_boundary.gpkg",
        preparation / "segment_relation_graph_road_poi_order.gpickle",
        preparation / "segment_order_od_hourly.csv",
        output.with_suffix(".png"),
        output.with_suffix(".pdf"),
    )


if __name__ == "__main__":
    main()
