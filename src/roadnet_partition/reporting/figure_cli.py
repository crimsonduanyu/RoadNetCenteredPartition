"""Shared boundary arguments for the figure entrypoints (AUD-007).

Both figure CLIs take the same explicit boundary binding, so the argument
definitions and the resolution order live here rather than being restated — and
able to drift — in each script.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from roadnet_partition.io import environment as _environment  # noqa: F401
import geopandas as gpd

from roadnet_partition.reporting.boundary_contract import (
    MANIFEST_LOGICAL_NAME,
    SUPPORTED_BOUNDARY_SUFFIXES,
    BoundaryContractError,
)
from roadnet_partition.reporting.boundary_resolver import (
    boundary_path_from_manifest,
    resolve_figure_boundary,
)


REFUSAL_EXIT_CODE = 2


def add_boundary_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the required, explicit study-area boundary binding to ``parser``."""

    supported = ", ".join(SUPPORTED_BOUNDARY_SUFFIXES)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--boundary",
        type=Path,
        help=(
            "Path to the study-area boundary polygon to draw. Supported formats: "
            f"{supported}. Must be a Polygon/MultiPolygon layer with a declared CRS, "
            "and must cover the run's partition. No directory is searched and no "
            "default is applied."
        ),
    )
    group.add_argument(
        "--boundary-from-manifest",
        action="store_true",
        help=(
            "Use the boundary the named run recorded as its "
            f"{MANIFEST_LOGICAL_NAME!r} input. The recorded size and SHA-256 are "
            "re-verified, so a run always renders against its own study area."
        ),
    )
    parser.add_argument(
        "--boundary-layer",
        help=(
            "Layer to read from a multi-layer boundary file (.gpkg only). Required "
            "when the file holds more than one polygon layer; the candidates are "
            "listed on failure. A layer is never chosen automatically."
        ),
    )


def load_run_manifest(run: Path) -> dict[str, Any]:
    """Read the manifest of an explicitly named run."""

    manifest_path = run / "manifest.json"
    if not manifest_path.is_file():
        raise BoundaryContractError(f"{run}: run manifest not found at {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def resolve_boundary(
    args: argparse.Namespace,
    *,
    run: Path,
    manifest: Mapping[str, Any],
    partition: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Return the validated boundary for one figure invocation.

    Exactly one binding is honoured, and the failure of an explicit input is
    never followed by a search for another candidate.
    """

    record: Mapping[str, Any] | None = None
    if args.boundary_from_manifest:
        path = boundary_path_from_manifest(manifest, run_dir=run)
        files = manifest.get("inputs", {}).get("files", {})
        record = files[MANIFEST_LOGICAL_NAME]
    else:
        path = Path(args.boundary)

    return resolve_figure_boundary(
        boundary_path=path,
        partition=partition,
        layer=args.boundary_layer,
        record=record,
    )


__all__ = [
    "REFUSAL_EXIT_CODE",
    "add_boundary_arguments",
    "load_run_manifest",
    "resolve_boundary",
]
