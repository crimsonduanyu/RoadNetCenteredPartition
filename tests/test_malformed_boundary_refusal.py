"""R6.1.1: a boundary that cannot be read is refused like any other violation.

R6.1 made the study-area boundary an explicit, validated input, but only
*contract* violations were normalized. Bytes that no driver can parse — a
truncated GeoPackage, a half-written GeoJSON, a shapefile missing its sidecars —
escaped as a pyogrio/Fiona ``DataSourceError``, so the CLI printed a driver
traceback and exited 1 instead of refusing cleanly and exiting 2.

These tests pin the normalized behaviour: every unreadable artifact becomes a
``BoundaryContractError``, both entrypoints exit 2 with a ``refused:`` line and
no traceback, nothing is written, and no other file or format is tried. The
suite deliberately mixes real corrupt files with monkeypatched reader failures,
so it cannot pass on mocks alone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

import geopandas as gpd
import pytest

from roadnet_partition.reporting import boundary_resolver
from roadnet_partition.reporting.boundary_contract import BoundaryContractError
from roadnet_partition.reporting.boundary_resolver import resolve_figure_boundary

from test_figure_boundary_selection import (
    FIGURE_SCRIPTS,
    MANIFEST_LOGICAL_NAME,
    assert_no_output,
    boundary_frame,
    build_run,
    partition_frame,
    run_cli,
    write_boundary,
)

# The refusal wording every unreadable artifact must produce.
READ_REFUSAL = "could not be read as a supported geospatial dataset"


# ---------------------------------------------------------------------------
# Malformed fixtures, each built in a temporary directory. No corrupt binary is
# committed; every case is generated from a valid file at test time.
# ---------------------------------------------------------------------------

def zero_byte_gpkg(tmp_path: Path) -> Path:
    path = tmp_path / "zero_byte.gpkg"
    path.write_bytes(b"")
    return path


def text_disguised_as_gpkg(tmp_path: Path) -> Path:
    path = tmp_path / "text_disguised.gpkg"
    path.write_text("this is definitely not a geopackage\n" * 8, encoding="utf-8")
    return path


def truncated_gpkg(tmp_path: Path) -> Path:
    source = write_boundary(tmp_path / "source_for_truncation.gpkg")
    data = source.read_bytes()
    path = tmp_path / "truncated.gpkg"
    path.write_bytes(data[: max(1, len(data) // 3)])
    return path


def gpkg_without_layer_catalog(tmp_path: Path) -> Path:
    """A container that opens as SQLite but has lost its GeoPackage catalog."""
    path = write_boundary(tmp_path / "no_catalog.gpkg")
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE gpkg_contents")
        connection.commit()
    finally:
        connection.close()
    return path


def malformed_geojson(tmp_path: Path) -> Path:
    path = tmp_path / "malformed.geojson"
    path.write_text('{"type": "FeatureCollection", "features": [ {"type": ', encoding="utf-8")
    return path


def incomplete_shapefile(tmp_path: Path) -> Path:
    """A .shp whose required .shx/.dbf sidecars are absent."""
    directory = tmp_path / "incomplete_shp"
    directory.mkdir()
    path = directory / "boundary.shp"
    boundary_frame().to_file(path, driver="ESRI Shapefile")
    for suffix in (".shx", ".dbf"):
        sidecar = path.with_suffix(suffix)
        if sidecar.exists():
            sidecar.unlink()
    return path


MALFORMED_CASES = {
    "zero_byte_gpkg": zero_byte_gpkg,
    "text_disguised_as_gpkg": text_disguised_as_gpkg,
    "truncated_gpkg": truncated_gpkg,
    "gpkg_without_layer_catalog": gpkg_without_layer_catalog,
    "malformed_geojson": malformed_geojson,
    "incomplete_shapefile": incomplete_shapefile,
}


# ---------------------------------------------------------------------------
# Direct resolver API
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", sorted(MALFORMED_CASES))
def test_every_unreadable_artifact_becomes_a_contract_error(case: str, tmp_path: Path) -> None:
    """The resolver raises the contract error, never a raw driver exception."""
    path = MALFORMED_CASES[case](tmp_path)
    with pytest.raises(BoundaryContractError, match=READ_REFUSAL):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


@pytest.mark.parametrize("case", sorted(MALFORMED_CASES))
def test_the_originating_driver_error_is_preserved_for_debugging(
    case: str, tmp_path: Path
) -> None:
    """``raise ... from error`` keeps the cause even though the CLI hides it."""
    path = MALFORMED_CASES[case](tmp_path)
    with pytest.raises(BoundaryContractError) as caught:
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())
    assert caught.value.__cause__ is not None
    assert isinstance(caught.value.__cause__, boundary_resolver.READ_ERROR_TYPES)


@pytest.mark.parametrize("case", sorted(MALFORMED_CASES))
def test_a_refusal_never_repeats_the_driver_text(case: str, tmp_path: Path) -> None:
    """GDAL messages can carry absolute paths and connection details."""
    path = MALFORMED_CASES[case](tmp_path)
    with pytest.raises(BoundaryContractError) as caught:
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())

    message = str(caught.value)
    driver_text = str(caught.value.__cause__)
    # The only path in the message is the one the caller supplied.
    assert message.startswith(str(path))
    assert driver_text not in message
    for leaked in ("not recognized as being in a supported file format",
                   "It might help to specify", "sqlite3_prepare_v2", "Permission denied"):
        assert leaked not in message
    # The file's own bytes are never echoed back.
    assert "not a geopackage" not in message


def test_an_unreadable_file_is_refused_rather_than_reported_as_missing(tmp_path: Path) -> None:
    """A permission failure is an OSError, not a contract-shaped absence."""
    path = write_boundary(tmp_path / "unreadable.gpkg")
    os.chmod(path, 0o000)
    if os.access(path, os.R_OK):  # running as root; the case cannot be built
        pytest.skip("cannot make a file unreadable for this user")
    try:
        with pytest.raises(BoundaryContractError, match=READ_REFUSAL):
            resolve_figure_boundary(boundary_path=path, partition=partition_frame())
    finally:
        os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Monkeypatched reader failures — the two distinct read sites
# ---------------------------------------------------------------------------

def _data_source_error(message: str = "simulated driver failure") -> BaseException:
    from pyogrio.errors import DataSourceError

    return DataSourceError(message)


def test_a_failure_listing_the_layer_catalog_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_boundary(tmp_path / "boundary.gpkg")

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise _data_source_error("catalog unreadable")

    monkeypatch.setattr(boundary_resolver.gpd, "list_layers", explode)
    with pytest.raises(BoundaryContractError, match=READ_REFUSAL):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


def test_a_failure_reading_the_selected_layer_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog can be readable while the layer itself is not."""
    path = write_boundary(tmp_path / "boundary.gpkg")

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise _data_source_error("layer unreadable")

    monkeypatch.setattr(boundary_resolver.gpd, "read_file", explode)
    with pytest.raises(BoundaryContractError, match=READ_REFUSAL):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


def test_an_oserror_from_the_reader_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_boundary(tmp_path / "boundary.gpkg")

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("simulated I/O failure")

    monkeypatch.setattr(boundary_resolver.gpd, "read_file", explode)
    with pytest.raises(BoundaryContractError, match=READ_REFUSAL):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


def test_a_unicode_error_from_the_reader_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_boundary(tmp_path / "boundary.gpkg")

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(boundary_resolver.gpd, "read_file", explode)
    with pytest.raises(BoundaryContractError, match=READ_REFUSAL):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


# ---------------------------------------------------------------------------
# Contract errors must keep their own, more specific wording
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (lambda p: p / "absent.gpkg", "does not exist"),
        (lambda p: write_boundary(p / "wrong.txt".replace("txt", "gpkg")), None),
    ],
    ids=["missing_file", "valid_file"],
)
def test_a_readable_input_is_not_described_as_unreadable(
    builder: Any, expected: str | None, tmp_path: Path
) -> None:
    """A normal contract error is never re-wrapped into the vaguer message."""
    path = builder(tmp_path)
    if expected is None:
        resolved = resolve_figure_boundary(boundary_path=path, partition=partition_frame())
        assert not resolved.empty
        return
    with pytest.raises(BoundaryContractError) as caught:
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())
    assert expected in str(caught.value)
    assert READ_REFUSAL not in str(caught.value)


def test_an_unsupported_suffix_still_reports_the_format_rule(tmp_path: Path) -> None:
    path = tmp_path / "boundary.txt"
    path.write_text("not geospatial at all", encoding="utf-8")
    with pytest.raises(BoundaryContractError, match="unsupported boundary format"):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


# ---------------------------------------------------------------------------
# Both CLIs: exit 2, refused:, no traceback, no partial output
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
@pytest.mark.parametrize("case", sorted(MALFORMED_CASES))
def test_both_entrypoints_refuse_an_unreadable_boundary(
    script: str, case: str, tmp_path: Path
) -> None:
    good = write_boundary(tmp_path / "correct_boundary.gpkg")
    run = build_run(tmp_path, boundary=good)
    malformed = MALFORMED_CASES[case](tmp_path)
    output_dir = tmp_path / "figures"

    result = run_cli(script, run, output_dir, "--boundary", str(malformed))

    assert result.returncode == 2, result.stderr
    assert result.stderr.lstrip().startswith("refused:")
    assert READ_REFUSAL in result.stderr
    assert "Traceback" not in result.stderr
    assert_no_output(output_dir)


@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
def test_a_refused_boundary_does_not_fall_back_to_the_correct_one(
    script: str, tmp_path: Path
) -> None:
    """An unreadable input must not send the CLI looking for another file."""
    good = write_boundary(tmp_path / "correct_boundary.gpkg")
    run = build_run(tmp_path, boundary=good)
    malformed = truncated_gpkg(tmp_path)
    output_dir = tmp_path / "figures"

    result = run_cli(script, run, output_dir, "--boundary", str(malformed))

    assert result.returncode == 2
    # The valid boundary sits beside it and is bound by the manifest, yet the
    # refusal stands and nothing is drawn.
    assert_no_output(output_dir)
    assert str(good) not in result.stderr


@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
def test_a_manifest_bound_boundary_corrupted_after_the_run_is_refused(
    script: str, tmp_path: Path
) -> None:
    """The manifest path is honoured, and its unreadable bytes are refused."""
    boundary = write_boundary(tmp_path / "recorded_boundary.gpkg")
    run = build_run(tmp_path, boundary=boundary)
    # Rewrite the recorded file with unreadable bytes of the same length, so the
    # size check passes and the failure has to come from the read itself.
    original = boundary.read_bytes()
    boundary.write_bytes(b"\x00" * len(original))
    record = json.loads((run / "manifest.json").read_text())["inputs"]["files"]
    assert record[MANIFEST_LOGICAL_NAME]["size"] == len(original)
    output_dir = tmp_path / "figures"

    result = run_cli(script, run, output_dir, "--boundary-from-manifest")

    assert result.returncode == 2, result.stderr
    assert result.stderr.lstrip().startswith("refused:")
    assert "Traceback" not in result.stderr
    assert_no_output(output_dir)


@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
def test_a_valid_boundary_still_renders_after_the_change(
    script: str, tmp_path: Path
) -> None:
    """The normalization must not turn a good input into a refusal."""
    boundary = write_boundary(tmp_path / "correct_boundary.gpkg")
    run = build_run(tmp_path, boundary=boundary)
    output_dir = tmp_path / "figures"

    result = run_cli(script, run, output_dir, "--boundary-from-manifest")

    assert result.returncode == 0, result.stderr
    produced = sorted(path.name for path in output_dir.glob("*"))
    assert produced, "a valid boundary must still render"


def test_the_read_boundary_names_concrete_driver_exceptions() -> None:
    """The narrow except must not degenerate into catching everything."""
    assert boundary_resolver.READ_ERROR_TYPES
    assert Exception not in boundary_resolver.READ_ERROR_TYPES
    assert BaseException not in boundary_resolver.READ_ERROR_TYPES
    # A contract error must never be swallowed by the read boundary, even
    # though Fiona's DriverError is also a ValueError.
    assert not issubclass(BoundaryContractError, boundary_resolver.READ_ERROR_TYPES)


def test_the_resolver_source_does_not_use_a_blanket_except() -> None:
    source = Path(boundary_resolver.__file__).read_text(encoding="utf-8")
    assert "except Exception" not in source
    assert "except BaseException" not in source
    assert "except:" not in source
