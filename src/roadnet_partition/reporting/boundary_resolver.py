"""Deterministic resolution of the figure map boundary (AUD-007).

``resolve_figure_boundary`` is the single entry point every figure uses to
obtain its study-area boundary. It accepts exactly one explicit binding — a
path, or a run-manifest logical name — and validates it in a fixed order before
returning a canonical GeoDataFrame.

The function never searches. It does not glob, rank candidates by name or
mtime, pick a first match or a first layer, or look at a neighbouring file after
an explicit input fails. If the named artifact does not satisfy
``BoundaryArtifactV1`` the call raises; nothing else is tried.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from roadnet_partition.io import environment as _environment  # noqa: F401
import geopandas as gpd

from roadnet_partition.io.geospatial import project_gdf
from roadnet_partition.io.manifests import sha256_file
from roadnet_partition.reporting.boundary_contract import (
    CONTAINMENT_TOLERANCE,
    MANIFEST_LOGICAL_NAME,
    ALLOWED_GEOMETRY_TYPES,
    BoundaryContractError,
    check_crs,
    check_finite_bounds,
    check_geometry,
    check_regular_file,
    check_suffix,
    supports_layers,
)


def boundary_path_from_manifest(manifest: Mapping[str, Any], *, run_dir: Path) -> Path:
    """Return the boundary path the run manifest binds to this run.

    The binding is a single named lookup. A manifest that does not carry the
    record is refused rather than answered from somewhere else, so an older run
    fails loudly and the caller can pass ``--boundary`` deliberately.
    """

    files = manifest.get("inputs", {}).get("files", {})
    if not isinstance(files, Mapping) or MANIFEST_LOGICAL_NAME not in files:
        available = sorted(files) if isinstance(files, Mapping) else []
        raise BoundaryContractError(
            f"{run_dir}: run manifest records no {MANIFEST_LOGICAL_NAME!r} input, so the "
            f"boundary cannot be bound to this run; pass --boundary explicitly. "
            f"Recorded inputs: {available}"
        )
    record = files[MANIFEST_LOGICAL_NAME]
    if not isinstance(record, Mapping) or not record.get("path"):
        raise BoundaryContractError(
            f"{run_dir}: run manifest {MANIFEST_LOGICAL_NAME!r} record has no path"
        )
    return Path(str(record["path"]))


def _verify_record(path: Path, record: Mapping[str, Any]) -> None:
    """Refuse a boundary whose bytes differ from the ones the run recorded."""

    expected_size = record.get("size")
    if expected_size is not None and path.stat().st_size != expected_size:
        raise BoundaryContractError(
            f"{path}: boundary size {path.stat().st_size} does not match the "
            f"{expected_size} recorded for {MANIFEST_LOGICAL_NAME}"
        )
    expected_digest = record.get("sha256")
    if expected_digest is not None:
        actual = sha256_file(path)
        if actual != expected_digest:
            raise BoundaryContractError(
                f"{path}: boundary sha256 {actual} does not match the "
                f"{expected_digest} recorded for {MANIFEST_LOGICAL_NAME}"
            )


def _polygonal_layers(path: Path) -> list[str]:
    """List layers whose declared geometry type is polygonal.

    Eligibility is decided by declared type rather than position, so the answer
    does not depend on the order layers sit in the container.
    """

    listed = gpd.list_layers(path)
    return [
        str(name)
        for name, geometry_type in zip(listed["name"], listed["geometry_type"])
        if str(geometry_type) in ALLOWED_GEOMETRY_TYPES
    ]


def _select_layer(path: Path, requested: str | None) -> str | None:
    """Resolve the layer to read, refusing ambiguity instead of guessing."""

    if not supports_layers(path.name):
        if requested is not None:
            raise BoundaryContractError(
                f"{path}: --boundary-layer is not valid for {path.suffix}, which holds one layer"
            )
        return None

    listed = gpd.list_layers(path)
    all_names = [str(name) for name in listed["name"]]
    eligible = _polygonal_layers(path)

    if requested is not None:
        if requested not in all_names:
            raise BoundaryContractError(
                f"{path}: layer {requested!r} does not exist; available layers: {all_names}"
            )
        if requested not in eligible:
            raise BoundaryContractError(
                f"{path}: layer {requested!r} is not polygonal; polygonal layers: {eligible}"
            )
        return requested

    if not eligible:
        raise BoundaryContractError(
            f"{path}: no polygonal layer to use as a study-area boundary; layers: {all_names}"
        )
    if len(eligible) > 1:
        raise BoundaryContractError(
            f"{path}: {len(eligible)} polygonal layers are candidates, so the boundary is "
            f"ambiguous; pass --boundary-layer naming one of: {eligible}"
        )
    return eligible[0]


def check_spatial_consistency(
    boundary: gpd.GeoDataFrame,
    partition: gpd.GeoDataFrame,
    *,
    source: Any,
    tolerance: float = CONTAINMENT_TOLERANCE,
) -> None:
    """Refuse a boundary that does not describe the partition's study area.

    The partition is never modified. The comparison is by length outside the
    boundary rather than strict containment, because reprojection and clipping
    the network at the study edge both move geometry slightly; a boundary from a
    different study area is far outside the tolerance.
    """

    check_finite_bounds(boundary, label=f"{source}: boundary")
    check_finite_bounds(partition, label=f"{source}: partition")

    area = boundary.geometry.union_all()
    if not partition.geometry.intersects(area).any():
        raise BoundaryContractError(
            f"{source}: boundary does not intersect the partition, so it describes a "
            f"different study area"
        )
    total = float(partition.geometry.length.sum())
    if total <= 0.0:
        return
    outside = float(partition.geometry.difference(area).length.sum())
    fraction = outside / total
    if fraction > tolerance:
        raise BoundaryContractError(
            f"{source}: {fraction:.1%} of the partition falls outside the boundary "
            f"(tolerance {tolerance:.1%}), so this boundary does not belong to this run"
        )


def resolve_figure_boundary(
    *,
    boundary_path: Path,
    partition: gpd.GeoDataFrame,
    layer: str | None = None,
    record: Mapping[str, Any] | None = None,
) -> gpd.GeoDataFrame:
    """Validate and load the boundary for a figure, or raise.

    ``record`` is the run-manifest file record when the boundary was bound
    through a manifest, and supplies the size/hash to re-verify.

    The returned frame is in the partition's CRS so callers draw one coordinate
    system. Raises :class:`BoundaryContractError` before any output exists.
    """

    path = Path(boundary_path)

    # Path and format, before the file is opened.
    check_suffix(path)
    check_regular_file(path)

    # File record, so a boundary that changed since the run is refused.
    if record is not None:
        _verify_record(path, record)

    # Layer selection from declared metadata, never by position.
    selected = _select_layer(path, layer)
    source = path if selected is None else f"{path}[{selected}]"

    boundary = gpd.read_file(path) if selected is None else gpd.read_file(path, layer=selected)

    # Content: CRS, then geometry type, then non-empty/validity.
    check_crs(boundary, source=source)
    check_geometry(boundary, source=source)

    if partition.crs is None:
        raise BoundaryContractError(
            f"{source}: the partition has no CRS, so the boundary cannot be placed against it"
        )
    boundary = project_gdf(boundary, str(partition.crs))

    # Spatial agreement with what is actually being drawn.
    check_spatial_consistency(boundary, partition, source=source)
    return boundary


__all__ = [
    "boundary_path_from_manifest",
    "check_spatial_consistency",
    "resolve_figure_boundary",
]
