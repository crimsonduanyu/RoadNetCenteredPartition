import argparse
import sys
from pathlib import Path

import geopandas as gpd

from roadnet_partition.pipeline.preparation import output_paths as preparation_output_paths
from roadnet_partition.pipeline.stages import canonical_partition_output_key
from roadnet_partition.reporting.best_partition_map import render_partition_order_figure
from roadnet_partition.reporting.boundary_contract import BoundaryContractError
from roadnet_partition.reporting.figure_cli import (
    REFUSAL_EXIT_CODE,
    add_boundary_arguments,
    load_run_manifest,
    resolve_boundary,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Render the two-panel partition/order figure from one run. The "
            "study-area boundary is an explicit input: pass --boundary, or "
            "--boundary-from-manifest to use the boundary the run itself recorded."
        ),
    )
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "outputs/figures")
    add_boundary_arguments(parser)
    args = parser.parse_args()

    run = args.run.resolve()
    manifest = load_run_manifest(run)
    outputs = manifest["stages"]["partition"]["outputs"]
    partition = Path(outputs[canonical_partition_output_key(outputs)]["path"])
    preparation = run / "preparation"

    # Resolved and fully validated before the output directory is touched.
    boundary = resolve_boundary(
        args, run=run, manifest=manifest, partition=gpd.read_file(partition),
    )

    output = args.output_dir.resolve() / "partition_and_mean_hourly_orders"
    render_partition_order_figure(
        partition,
        preparation / "road_edges_classified.gpkg",
        boundary,
        preparation_output_paths(preparation)["graph"],
        preparation / "segment_order_od_hourly.csv",
        output.with_suffix(".png"),
        output.with_suffix(".pdf"),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BoundaryContractError as error:
        print(f"refused: {error}", file=sys.stderr)
        sys.exit(REFUSAL_EXIT_CODE)
