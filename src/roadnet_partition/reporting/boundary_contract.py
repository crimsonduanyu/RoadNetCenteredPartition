"""BoundaryArtifactV1 — the explicit map-boundary contract for figures (AUD-007).

A publication figure states a study area. If the boundary it draws is not the
boundary the run was computed for, the figure is wrong in a way no reader can
detect, so the boundary has to be an explicit, validated, deterministic input
rather than something the code picks on the caller's behalf.

This module holds only the predicates and the vocabulary. The resolver that
sequences them lives in :mod:`roadnet_partition.reporting.boundary_resolver`,
and no function here reads a directory, ranks candidates, or falls back to a
different file: every check answers "is *this* named artifact acceptable?"
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from roadnet_partition.io import environment as _environment  # noqa: F401
import geopandas as gpd


CONTRACT_NAME = "BoundaryArtifactV1"

#: Vector formats the figure boundary may use. Anything else is refused by
#: name, because the driver a suffix selects decides how the bytes are parsed.
SUPPORTED_BOUNDARY_SUFFIXES = (".gpkg", ".geojson", ".json", ".shp")

#: Multi-layer container formats. For these a layer must be selectable; for
#: single-layer formats a layer may not be requested at all.
MULTI_LAYER_SUFFIXES = (".gpkg",)

#: The only geometry types a study-area boundary may consist of. A ring drawn
#: as lines, a road segment, or a centroid are all rejected here, which is what
#: stops a non-boundary layer from being drawn as if it were the study area.
ALLOWED_GEOMETRY_TYPES = ("Polygon", "MultiPolygon")

#: The run-manifest logical name that records the boundary a run was prepared
#: with. Preparation registers its boundary input under this key, so it is the
#: only binding that can tie a figure back to the run that produced it.
MANIFEST_LOGICAL_NAME = "preparation.boundary"

#: Fraction of the partition extent that may fall outside the boundary before
#: the pair is treated as a scope mismatch. Reprojection and the ordinary
#: cartographic practice of clipping a network at the study-area edge both move
#: geometry by small amounts, so an exact-containment rule would reject correct
#: inputs; a gross mismatch such as a different ring road is far outside this.
CONTAINMENT_TOLERANCE = 0.02


class BoundaryContractError(ValueError):
    """Raised when a boundary input does not satisfy ``BoundaryArtifactV1``.

    The figure entrypoints turn this into a short refusal message and a
    non-zero exit code instead of a traceback.
    """


def is_supported_boundary_suffix(name: str) -> bool:
    """Report whether ``name`` ends in a supported vector-format suffix."""

    return Path(name).suffix.lower() in SUPPORTED_BOUNDARY_SUFFIXES


def supports_layers(name: str) -> bool:
    """Report whether ``name`` uses a format that can hold several layers."""

    return Path(name).suffix.lower() in MULTI_LAYER_SUFFIXES


def check_suffix(path: Path) -> None:
    """Refuse a boundary whose suffix is not an explicitly supported format."""

    if not is_supported_boundary_suffix(path.name):
        supported = ", ".join(SUPPORTED_BOUNDARY_SUFFIXES)
        raise BoundaryContractError(
            f"{path}: unsupported boundary format {path.suffix or '(none)'!r}; "
            f"{CONTRACT_NAME} accepts {supported}"
        )


def check_regular_file(path: Path) -> None:
    """Refuse anything that is not an existing, non-symlink regular file.

    The symlink rule matches the rest of the repository's path handling: a
    boundary reached through a link is not the file the manifest recorded.
    """

    if path.is_symlink():
        raise BoundaryContractError(f"{path}: boundary path is a symbolic link")
    if not path.exists():
        raise BoundaryContractError(f"{path}: boundary file does not exist")
    if not path.is_file():
        raise BoundaryContractError(f"{path}: boundary path is not a regular file")


def check_geometry(boundary: gpd.GeoDataFrame, *, source: Any) -> None:
    """Refuse a boundary frame that is empty, null, invalid, or not polygonal."""

    if boundary.empty:
        raise BoundaryContractError(f"{source}: boundary contains no features")
    if boundary.geometry.isna().any():
        raise BoundaryContractError(f"{source}: boundary contains a null geometry")
    if boundary.geometry.is_empty.any():
        raise BoundaryContractError(f"{source}: boundary contains an empty geometry")
    present = sorted(set(boundary.geom_type))
    unsupported = [name for name in present if name not in ALLOWED_GEOMETRY_TYPES]
    if unsupported:
        allowed = "/".join(ALLOWED_GEOMETRY_TYPES)
        raise BoundaryContractError(
            f"{source}: boundary geometry type(s) {unsupported} are not a study-area "
            f"polygon; {CONTRACT_NAME} requires {allowed}"
        )
    invalid = [
        index for index, geometry in zip(boundary.index, boundary.geometry) if not geometry.is_valid
    ]
    if invalid:
        raise BoundaryContractError(f"{source}: boundary polygon is invalid at row(s) {invalid}")


def check_crs(boundary: gpd.GeoDataFrame, *, source: Any) -> None:
    """Refuse a boundary with no CRS.

    Without a declared CRS the coordinates cannot be placed, and guessing one
    is exactly how a geographic boundary ends up drawn over projected metres.
    """

    if boundary.crs is None:
        raise BoundaryContractError(
            f"{source}: boundary has no CRS; {CONTRACT_NAME} requires a declared CRS"
        )


def check_finite_bounds(frame: gpd.GeoDataFrame, *, label: str) -> tuple[float, ...]:
    """Return ``frame``'s bounds, refusing non-finite extents."""

    bounds = tuple(float(value) for value in frame.total_bounds)
    if not all(value == value and abs(value) != float("inf") for value in bounds):
        raise BoundaryContractError(f"{label} bounds are not finite: {bounds}")
    return bounds


__all__ = [
    "ALLOWED_GEOMETRY_TYPES",
    "CONTAINMENT_TOLERANCE",
    "CONTRACT_NAME",
    "MANIFEST_LOGICAL_NAME",
    "MULTI_LAYER_SUFFIXES",
    "SUPPORTED_BOUNDARY_SUFFIXES",
    "BoundaryContractError",
    "check_crs",
    "check_finite_bounds",
    "check_geometry",
    "check_regular_file",
    "check_suffix",
    "is_supported_boundary_suffix",
    "supports_layers",
]
