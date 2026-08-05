"""Gate B unit tests for the BoundaryArtifactV1 predicates.

AUD-007 remediation. These cover the individual contract checks in isolation;
the resolver that sequences them, the CLI, and the no-partial-output guarantees
are covered by ``tests/test_figure_boundary_selection.py``.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import GeometryCollection, LineString, Point, Polygon

from roadnet_partition.reporting.boundary_contract import (
    ALLOWED_GEOMETRY_TYPES,
    CONTRACT_NAME,
    MANIFEST_LOGICAL_NAME,
    SUPPORTED_BOUNDARY_SUFFIXES,
    BoundaryContractError,
    check_crs,
    check_finite_bounds,
    check_geometry,
    check_regular_file,
    check_suffix,
    is_supported_boundary_suffix,
    supports_layers,
)


CRS = "EPSG:32650"


def square(size: float = 10.0, offset: float = 0.0) -> Polygon:
    return Polygon([
        (offset, offset), (offset + size, offset),
        (offset + size, offset + size), (offset, offset + size),
    ])


def frame(*geometries: object, crs: str | None = CRS) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": [f"g{i}" for i in range(len(geometries))]},
                            geometry=list(geometries), crs=crs)


def test_the_contract_names_the_only_binding_and_geometry_types_it_accepts() -> None:
    """The vocabulary is frozen here so a later change is a visible diff."""
    assert CONTRACT_NAME == "BoundaryArtifactV1"
    assert MANIFEST_LOGICAL_NAME == "preparation.boundary"
    assert ALLOWED_GEOMETRY_TYPES == ("Polygon", "MultiPolygon")
    assert SUPPORTED_BOUNDARY_SUFFIXES == (".gpkg", ".geojson", ".json", ".shp")


@pytest.mark.parametrize("name", ["a.gpkg", "a.geojson", "a.json", "a.shp", "A.GPKG"])
def test_supported_suffixes_are_accepted_case_insensitively(name: str) -> None:
    assert is_supported_boundary_suffix(name)
    check_suffix(Path(name))


@pytest.mark.parametrize("name", ["a.gpickle", "a.pkl", "a.csv", "a.tif", "a", "a.gpkg.bak"])
def test_unsupported_suffixes_are_refused_by_name(name: str) -> None:
    assert not is_supported_boundary_suffix(name)
    with pytest.raises(BoundaryContractError, match="unsupported boundary format"):
        check_suffix(Path(name))


def test_only_geopackage_is_treated_as_a_multi_layer_container() -> None:
    assert supports_layers("a.gpkg")
    assert not supports_layers("a.geojson")
    assert not supports_layers("a.shp")


def test_a_missing_file_a_directory_and_a_symlink_are_each_refused(tmp_path: Path) -> None:
    missing = tmp_path / "absent.gpkg"
    with pytest.raises(BoundaryContractError, match="does not exist"):
        check_regular_file(missing)

    directory = tmp_path / "dir.gpkg"
    directory.mkdir()
    with pytest.raises(BoundaryContractError, match="not a regular file"):
        check_regular_file(directory)

    real = tmp_path / "real.gpkg"
    real.write_bytes(b"x")
    link = tmp_path / "link.gpkg"
    link.symlink_to(real)
    with pytest.raises(BoundaryContractError, match="symbolic link"):
        check_regular_file(link)

    broken = tmp_path / "broken.gpkg"
    broken.symlink_to(tmp_path / "nowhere.gpkg")
    with pytest.raises(BoundaryContractError, match="symbolic link"):
        check_regular_file(broken)

    check_regular_file(real)


def test_polygon_multipolygon_holes_and_disconnected_parts_all_satisfy_the_contract() -> None:
    """A study area legitimately has holes and disconnected pieces."""
    hole = Polygon(
        [(0, 0), (10, 0), (10, 10), (0, 10)],
        [[(3, 3), (3, 6), (6, 6), (6, 3)]],
    )
    disconnected = square(2).union(square(2, offset=50))
    assert disconnected.geom_type == "MultiPolygon"
    check_geometry(frame(square()), source="s")
    check_geometry(frame(hole), source="s")
    check_geometry(frame(disconnected), source="s")
    check_geometry(frame(square(), square(2, offset=40)), source="s")


def test_an_empty_frame_is_refused() -> None:
    empty = gpd.GeoDataFrame({"name": []}, geometry=[], crs=CRS)
    with pytest.raises(BoundaryContractError, match="no features"):
        check_geometry(empty, source="s")


def test_null_and_empty_geometries_are_refused() -> None:
    with pytest.raises(BoundaryContractError, match="null geometry"):
        check_geometry(frame(None), source="s")
    with pytest.raises(BoundaryContractError, match="empty geometry"):
        check_geometry(frame(Polygon()), source="s")


@pytest.mark.parametrize(
    "geometry",
    [
        Point(1, 1),
        LineString([(0, 0), (1, 1)]),
        GeometryCollection([Point(0, 0), square()]),
    ],
    ids=["point", "linestring", "geometrycollection"],
)
def test_non_polygon_geometry_types_are_refused(geometry: object) -> None:
    """A ring drawn as lines or a centroid must never stand in for a boundary."""
    with pytest.raises(BoundaryContractError, match="not a study-area polygon"):
        check_geometry(frame(geometry), source="s")


def test_an_invalid_polygon_is_refused_rather_than_repaired() -> None:
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    assert not bowtie.is_valid
    with pytest.raises(BoundaryContractError, match="invalid at row"):
        check_geometry(frame(bowtie), source="s")


def test_a_missing_crs_is_refused_and_a_declared_crs_passes() -> None:
    with pytest.raises(BoundaryContractError, match="no CRS"):
        check_crs(frame(square(), crs=None), source="s")
    check_crs(frame(square()), source="s")


def test_finite_bounds_are_returned_and_non_finite_bounds_are_refused() -> None:
    assert check_finite_bounds(frame(square()), label="boundary") == (0.0, 0.0, 10.0, 10.0)
    empty = gpd.GeoDataFrame({"name": []}, geometry=[], crs=CRS)
    with pytest.raises(BoundaryContractError, match="not finite"):
        check_finite_bounds(empty, label="boundary")
