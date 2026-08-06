"""Gate E regression tests: the figure boundary is explicit and deterministic.

AUD-007 remediation. The best-partition figure CLIs used to overlay one
hard-coded Fifth Ring boundary on whatever run they were given, so a fourth-ring
run rendered against the wrong study area and exited zero. These tests pin the
migrated behaviour: the boundary is an explicit binding, every contract
violation is refused before any output exists, and the drawn result depends only
on the named artifact — never on filenames, directory contents, or iteration
order.

Group letters refer to the R6.1 Gate E matrix (A crafted regression, B path and
file safety, C geometry, D layer, E CRS, F spatial consistency, G no partial
output, H visual regression, I CLI).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import warnings

import geopandas as gpd
import matplotlib
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import GeometryCollection, LineString, Point, Polygon

from roadnet_partition.io.manifests import file_record
from roadnet_partition.io.safe_graph import write_safe_graph
from roadnet_partition.reporting import best_partition_map
from roadnet_partition.reporting.boundary_contract import (
    CONTAINMENT_TOLERANCE,
    MANIFEST_LOGICAL_NAME,
    BoundaryContractError,
)
from roadnet_partition.reporting.boundary_resolver import (
    boundary_path_from_manifest,
    check_spatial_consistency,
    resolve_figure_boundary,
)
from roadnet_partition.reporting.figure_cli import (
    add_boundary_arguments,
    load_run_manifest,
    resolve_boundary,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIGURE_SCRIPTS = (
    "scripts/figures/best_partition_maps.py",
    "scripts/figures/partition_order_panels.py",
)

CRS = "EPSG:32650"
OTHER_CRS = "EPSG:4326"

# A compact stand-in for a study area and the partition inside it. Real
# coordinates in metres so the projected-CRS rules behave as they do in
# production.
ORIGIN_X, ORIGIN_Y = 440_000.0, 4_410_000.0
EXTENT = 4_000.0


def study_area(scale: float = 1.0, shift: float = 0.0) -> Polygon:
    """A square study-area polygon around the fixture origin."""
    size = EXTENT * scale
    x, y = ORIGIN_X + shift, ORIGIN_Y + shift
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def boundary_frame(geometry: Any = None, *, crs: str | None = CRS) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["study_area"]},
        geometry=[study_area() if geometry is None else geometry],
        crs=crs,
    )


def partition_frame(crs: str | None = CRS, *, inset: float = 0.25) -> gpd.GeoDataFrame:
    """Four cluster segments comfortably inside :func:`study_area`."""
    low, high = ORIGIN_X + EXTENT * inset, ORIGIN_X + EXTENT * (1 - inset)
    segments, ids, clusters = [], [], []
    for index in range(4):
        x = low + (high - low) * index / 3
        segments.append(LineString([
            (x, ORIGIN_Y + EXTENT * inset), (x, ORIGIN_Y + EXTENT * (1 - inset)),
        ]))
        ids.append(f"s{index}")
        clusters.append(index % 2)
    return gpd.GeoDataFrame(
        {"seg_id": ids, "cluster_id": clusters}, geometry=segments, crs=crs,
    )


def write_boundary(path: Path, geometry: Any = None, *, crs: str | None = CRS,
                   layer: str | None = None) -> Path:
    frame = boundary_frame(geometry, crs=crs)
    if layer is None:
        frame.to_file(path)
    else:
        frame.to_file(path, layer=layer, driver="GPKG")
    return path


def namespace(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "boundary": None, "boundary_from_manifest": False, "boundary_layer": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


# ---------------------------------------------------------------------------
# A complete synthetic run, so the CLI can be executed end to end
# ---------------------------------------------------------------------------

def build_run(root: Path, *, boundary: Path, partition: gpd.GeoDataFrame | None = None) -> Path:
    """Create a completed-run layout whose manifest binds ``boundary``."""
    run = root / "run"
    preparation = run / "preparation"
    clusters_dir = run / "partition" / "clusters"
    preparation.mkdir(parents=True, exist_ok=True)
    clusters_dir.mkdir(parents=True, exist_ok=True)

    clusters = partition_frame() if partition is None else partition
    cluster_path = clusters_dir / "segment_clusters_road_poi_order_regularized_a.gpkg"
    clusters.to_file(cluster_path, driver="GPKG")

    gpd.GeoDataFrame(
        {"seg_id": list(clusters["seg_id"]), "segment_role": ["connector"] * len(clusters)},
        geometry=list(clusters.geometry), crs=clusters.crs,
    ).to_file(preparation / "road_edges_classified.gpkg", driver="GPKG")

    graph = nx.Graph()
    graph.add_nodes_from(clusters["seg_id"].astype(str))
    graph.add_edges_from([("s0", "s1"), ("s1", "s2"), ("s2", "s3")])
    write_safe_graph(graph, preparation / "segment_relation_graph_road_poi_order.graph.json.gz")

    pd.DataFrame({
        "slot_start": pd.to_datetime(["2019-06-01 08:00:00"] * len(clusters)),
        "origin_seg_id": list(clusters["seg_id"]),
        "dest_seg_id": list(clusters["seg_id"]),
        "order_count": [3, 5, 7, 9][: len(clusters)],
    }).to_csv(preparation / "segment_order_od_hourly.csv", index=False)

    (run / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "run_id": "fixture-run",
        "scope": "fixture",
        "status": "completed",
        "inputs": {"files": {MANIFEST_LOGICAL_NAME: file_record(boundary)}},
        "stages": {"partition": {"status": "completed", "outputs": {
            "cluster_gpkg_regularized_a": {"path": str(cluster_path.resolve())},
        }}},
    }, indent=2), encoding="utf-8")
    return run


def run_cli(script: str, run: Path, output_dir: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Invoke a figure entrypoint the way a user does, in a child process."""
    environment = dict(os.environ)
    environment.setdefault("GDAL_DATA", str(PROJECT_ROOT / ".conda/dydl/share/gdal"))
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT_ROOT / "src"), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / script),
         "--run", str(run), "--output-dir", str(output_dir), *arguments],
        capture_output=True, text=True, cwd=PROJECT_ROOT, env=environment,
    )


def assert_no_output(output_dir: Path) -> None:
    """Group G: a refusal leaves no figure, no stray file, and no directory."""
    if not output_dir.exists():
        return
    produced = sorted(path.name for path in output_dir.rglob("*"))
    assert produced == [], f"refused invocation produced {produced}"


@pytest.fixture()
def fixture_run(tmp_path: Path) -> tuple[Path, Path]:
    """A run plus the correct boundary its manifest binds."""
    boundary = write_boundary(tmp_path / "correct_boundary.gpkg")
    return build_run(tmp_path, boundary=boundary), boundary


# ---------------------------------------------------------------------------
# Group A — crafted AUD-007 regression
# ---------------------------------------------------------------------------

def test_the_correct_boundary_is_used_even_beside_similarly_named_wrong_ones(tmp_path: Path) -> None:
    """A: several candidates share one directory; only the named one is read."""
    shared = tmp_path / "boundaries"
    shared.mkdir()
    correct = write_boundary(shared / "study_area_boundary.gpkg")
    # Sorts before the correct file, and would win any first-match rule.
    write_boundary(shared / "aaa_wrong_boundary.gpkg", study_area(scale=40, shift=400_000))
    write_boundary(shared / "zzz_wrong_boundary.gpkg", study_area(scale=40, shift=400_000))

    partition = partition_frame()
    resolved = resolve_figure_boundary(boundary_path=correct, partition=partition)

    assert list(resolved.total_bounds) == list(boundary_frame().total_bounds)
    assert len(list(shared.iterdir())) == 3


def test_a_wrong_boundary_from_another_study_area_is_refused(tmp_path: Path) -> None:
    """A: the AUD-007 failure itself — a foreign boundary must not render."""
    wrong = write_boundary(tmp_path / "other_city.gpkg", study_area(shift=900_000))
    with pytest.raises(BoundaryContractError, match="does not intersect"):
        resolve_figure_boundary(boundary_path=wrong, partition=partition_frame())


@pytest.mark.parametrize("creation_order", [("correct", "wrong"), ("wrong", "correct")])
def test_file_creation_order_does_not_change_the_resolved_boundary(
    tmp_path: Path, creation_order: tuple[str, ...]
) -> None:
    """A: insertion order into the directory cannot influence the result."""
    shared = tmp_path / f"order_{'_'.join(creation_order)}"
    shared.mkdir()
    paths: dict[str, Path] = {}
    for name in creation_order:
        geometry = study_area() if name == "correct" else study_area(scale=40, shift=400_000)
        paths[name] = write_boundary(shared / f"{name}.gpkg", geometry)

    resolved = resolve_figure_boundary(boundary_path=paths["correct"], partition=partition_frame())
    assert list(resolved.total_bounds) == list(boundary_frame().total_bounds)


def test_resolution_is_stable_across_hash_seeds_and_iteration_order(tmp_path: Path) -> None:
    """A: the answer is a pure function of the named path, not of process state."""
    correct = write_boundary(tmp_path / "b.gpkg")
    partition = partition_frame()
    bounds = {
        tuple(resolve_figure_boundary(boundary_path=correct, partition=partition).total_bounds)
        for _ in range(5)
    }
    assert len(bounds) == 1


def test_neither_figure_script_names_a_boundary_file(tmp_path: Path) -> None:
    """A: the hard-coded Fifth Ring literal must not come back."""
    for relative in FIGURE_SCRIPTS:
        source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        assert "boundary.gpkg" not in source, f"{relative} names a boundary file"
        assert "fifth_ring" not in source, f"{relative} names a dataset scope"


@pytest.mark.parametrize("relative", FIGURE_SCRIPTS)
def test_the_figure_boundary_chain_performs_no_directory_discovery(relative: str) -> None:
    """A: no glob, rglob, iterdir, or first-candidate selection in the chain."""
    sources = [PROJECT_ROOT / relative] + [
        PROJECT_ROOT / "src/roadnet_partition/reporting" / name
        for name in ("figure_cli.py", "boundary_resolver.py", "boundary_contract.py")
    ]
    for path in sources:
        source = path.read_text(encoding="utf-8")
        for forbidden in (".glob(", ".rglob(", ".iterdir(", "os.listdir", "os.scandir", "getmtime"):
            assert forbidden not in source, f"{path.name} uses {forbidden}"


# ---------------------------------------------------------------------------
# Group B — path and file safety
# ---------------------------------------------------------------------------

def test_missing_directory_and_symlink_boundaries_are_refused(tmp_path: Path) -> None:
    partition = partition_frame()
    real = write_boundary(tmp_path / "real.gpkg")

    with pytest.raises(BoundaryContractError, match="does not exist"):
        resolve_figure_boundary(boundary_path=tmp_path / "absent.gpkg", partition=partition)

    directory = tmp_path / "dir.gpkg"
    directory.mkdir()
    with pytest.raises(BoundaryContractError, match="not a regular file"):
        resolve_figure_boundary(boundary_path=directory, partition=partition)

    link = tmp_path / "link.gpkg"
    link.symlink_to(real)
    with pytest.raises(BoundaryContractError, match="symbolic link"):
        resolve_figure_boundary(boundary_path=link, partition=partition)

    broken = tmp_path / "broken.gpkg"
    broken.symlink_to(tmp_path / "gone.gpkg")
    with pytest.raises(BoundaryContractError, match="symbolic link"):
        resolve_figure_boundary(boundary_path=broken, partition=partition)


def test_an_unsupported_suffix_is_refused_before_the_file_is_opened(tmp_path: Path) -> None:
    hostile = tmp_path / "boundary.gpickle"
    hostile.write_bytes(b"not opened")

    opened: list[str] = []
    real_open = Path.open

    def spy(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    Path.open = spy  # type: ignore[method-assign]
    try:
        with pytest.raises(BoundaryContractError, match="unsupported boundary format"):
            resolve_figure_boundary(boundary_path=hostile, partition=partition_frame())
    finally:
        Path.open = real_open  # type: ignore[method-assign]
    assert str(hostile) not in opened


def test_a_manifest_record_whose_hash_or_size_no_longer_matches_is_refused(tmp_path: Path) -> None:
    """B: a boundary edited after the run must not be drawn as that run's."""
    boundary = write_boundary(tmp_path / "b.gpkg")
    record = file_record(boundary)
    partition = partition_frame()

    resolve_figure_boundary(boundary_path=boundary, partition=partition, record=record)

    tampered = dict(record, sha256="0" * 64)
    with pytest.raises(BoundaryContractError, match="sha256"):
        resolve_figure_boundary(boundary_path=boundary, partition=partition, record=tampered)

    resized = dict(record, size=record["size"] + 1)
    with pytest.raises(BoundaryContractError, match="size"):
        resolve_figure_boundary(boundary_path=boundary, partition=partition, record=resized)


def test_a_boundary_replaced_after_the_run_is_detected_by_its_record(tmp_path: Path) -> None:
    boundary = write_boundary(tmp_path / "b.gpkg")
    record = file_record(boundary)
    boundary.unlink()
    write_boundary(boundary, study_area(scale=1.5))

    with pytest.raises(BoundaryContractError, match="size|sha256"):
        resolve_figure_boundary(
            boundary_path=boundary, partition=partition_frame(), record=record,
        )


def test_an_unreadable_boundary_fails_without_producing_a_frame(tmp_path: Path) -> None:
    """B: an unreadable artifact is a contract refusal, not a driver traceback."""
    corrupt = tmp_path / "corrupt.gpkg"
    corrupt.write_bytes(b"this is not a geopackage")
    with pytest.raises(BoundaryContractError, match="could not be read"):
        resolve_figure_boundary(boundary_path=corrupt, partition=partition_frame())


def test_a_manifest_without_the_boundary_record_names_the_missing_binding(tmp_path: Path) -> None:
    manifest = {"inputs": {"files": {"preparation.raw_edges": {"path": "x"}}}}
    with pytest.raises(BoundaryContractError, match=MANIFEST_LOGICAL_NAME):
        boundary_path_from_manifest(manifest, run_dir=tmp_path)


def test_a_manifest_record_without_a_path_is_refused(tmp_path: Path) -> None:
    manifest = {"inputs": {"files": {MANIFEST_LOGICAL_NAME: {"size": 1}}}}
    with pytest.raises(BoundaryContractError, match="no path"):
        boundary_path_from_manifest(manifest, run_dir=tmp_path)


# ---------------------------------------------------------------------------
# Group C — geometry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "geometry, expected",
    [
        (Point(ORIGIN_X, ORIGIN_Y), "not a study-area polygon"),
        (LineString([(ORIGIN_X, ORIGIN_Y), (ORIGIN_X + EXTENT, ORIGIN_Y)]), "not a study-area polygon"),
        (GeometryCollection([Point(ORIGIN_X, ORIGIN_Y), study_area()]), "not a study-area polygon"),
    ],
    ids=["point", "linestring", "geometrycollection"],
)
def test_non_polygon_boundaries_are_refused(tmp_path: Path, geometry: Any, expected: str) -> None:
    """C: a ring drawn as lines or a centroid is not a study area."""
    path = write_boundary(tmp_path / "wrong_type.geojson", geometry)
    with pytest.raises(BoundaryContractError, match=expected):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


def test_an_empty_boundary_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.gpkg"
    gpd.GeoDataFrame({"name": []}, geometry=[], crs=CRS).to_file(path, driver="GPKG")
    with pytest.raises(BoundaryContractError, match="no features|no polygonal layer"):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


def test_a_null_geometry_row_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "null.geojson"
    gpd.GeoDataFrame({"name": ["a"]}, geometry=[None], crs=CRS).to_file(path)
    with pytest.raises(BoundaryContractError, match="null geometry|no features"):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


def test_an_invalid_polygon_is_refused_and_not_repaired(tmp_path: Path) -> None:
    bowtie = Polygon([
        (ORIGIN_X, ORIGIN_Y), (ORIGIN_X + EXTENT, ORIGIN_Y + EXTENT),
        (ORIGIN_X + EXTENT, ORIGIN_Y), (ORIGIN_X, ORIGIN_Y + EXTENT),
    ])
    path = write_boundary(tmp_path / "invalid.geojson", bowtie)
    with pytest.raises(BoundaryContractError, match="invalid"):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


def test_polygons_with_holes_multipolygons_and_multiple_rows_are_accepted(tmp_path: Path) -> None:
    """C: legitimate study-area shapes must keep working."""
    partition = partition_frame()

    holed = Polygon(
        [(ORIGIN_X, ORIGIN_Y), (ORIGIN_X + EXTENT, ORIGIN_Y),
         (ORIGIN_X + EXTENT, ORIGIN_Y + EXTENT), (ORIGIN_X, ORIGIN_Y + EXTENT)],
        [[(ORIGIN_X + EXTENT * 0.45, ORIGIN_Y + EXTENT * 0.45),
          (ORIGIN_X + EXTENT * 0.45, ORIGIN_Y + EXTENT * 0.55),
          (ORIGIN_X + EXTENT * 0.55, ORIGIN_Y + EXTENT * 0.55),
          (ORIGIN_X + EXTENT * 0.55, ORIGIN_Y + EXTENT * 0.45)]],
    )
    resolve_figure_boundary(
        boundary_path=write_boundary(tmp_path / "holed.geojson", holed), partition=partition,
    )

    disconnected = study_area().union(study_area(scale=0.2, shift=EXTENT * 2))
    assert disconnected.geom_type == "MultiPolygon"
    resolve_figure_boundary(
        boundary_path=write_boundary(tmp_path / "multi.geojson", disconnected), partition=partition,
    )

    rows = gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[study_area(), study_area(scale=0.2, shift=EXTENT * 2)], crs=CRS,
    )
    rows_path = tmp_path / "rows.geojson"
    rows.to_file(rows_path)
    resolved = resolve_figure_boundary(boundary_path=rows_path, partition=partition)
    assert len(resolved) == 2


# ---------------------------------------------------------------------------
# Group D — layer selection
# ---------------------------------------------------------------------------

def test_a_single_polygon_layer_is_used_without_an_explicit_layer(tmp_path: Path) -> None:
    path = write_boundary(tmp_path / "single.gpkg", layer="study_area")
    resolved = resolve_figure_boundary(boundary_path=path, partition=partition_frame())
    assert len(resolved) == 1


def _two_layer_boundary(tmp_path: Path, name: str = "two.gpkg") -> Path:
    path = tmp_path / name
    boundary_frame().to_file(path, layer="correct_area", driver="GPKG")
    gpd.GeoDataFrame(
        {"name": ["other"]}, geometry=[study_area(scale=40, shift=400_000)], crs=CRS,
    ).to_file(path, layer="another_area", driver="GPKG")
    return path


def test_multiple_polygon_layers_are_ambiguous_and_list_every_candidate(tmp_path: Path) -> None:
    """D: never silently take the first layer."""
    path = _two_layer_boundary(tmp_path)
    with pytest.raises(BoundaryContractError) as error:
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())
    message = str(error.value)
    assert "ambiguous" in message
    assert "correct_area" in message and "another_area" in message


def test_an_explicit_layer_resolves_the_ambiguity(tmp_path: Path) -> None:
    path = _two_layer_boundary(tmp_path)
    resolved = resolve_figure_boundary(
        boundary_path=path, partition=partition_frame(), layer="correct_area",
    )
    assert list(resolved.total_bounds) == list(boundary_frame().total_bounds)


def test_an_explicit_wrong_layer_still_faces_the_other_checks(tmp_path: Path) -> None:
    path = _two_layer_boundary(tmp_path)
    with pytest.raises(BoundaryContractError, match="does not intersect"):
        resolve_figure_boundary(
            boundary_path=path, partition=partition_frame(), layer="another_area",
        )


def test_a_missing_layer_name_lists_the_available_layers(tmp_path: Path) -> None:
    path = _two_layer_boundary(tmp_path)
    with pytest.raises(BoundaryContractError) as error:
        resolve_figure_boundary(
            boundary_path=path, partition=partition_frame(), layer="absent",
        )
    assert "does not exist" in str(error.value)
    assert "correct_area" in str(error.value)


def test_a_non_polygon_layer_cannot_be_selected(tmp_path: Path) -> None:
    path = tmp_path / "mixed.gpkg"
    boundary_frame().to_file(path, layer="area", driver="GPKG")
    gpd.GeoDataFrame(
        {"name": ["road"]},
        geometry=[LineString([(ORIGIN_X, ORIGIN_Y), (ORIGIN_X + EXTENT, ORIGIN_Y)])], crs=CRS,
    ).to_file(path, layer="roads", driver="GPKG")

    with pytest.raises(BoundaryContractError, match="not polygonal"):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame(), layer="roads")

    # The single polygon layer is still unambiguous beside a line layer.
    resolved = resolve_figure_boundary(boundary_path=path, partition=partition_frame())
    assert len(resolved) == 1


def test_layer_write_order_does_not_change_which_layer_is_eligible(tmp_path: Path) -> None:
    """D: eligibility comes from declared type, not container position."""
    forward = tmp_path / "forward.gpkg"
    boundary_frame().to_file(forward, layer="area", driver="GPKG")
    gpd.GeoDataFrame(
        {"name": ["road"]},
        geometry=[LineString([(ORIGIN_X, ORIGIN_Y), (ORIGIN_X + EXTENT, ORIGIN_Y)])], crs=CRS,
    ).to_file(forward, layer="roads", driver="GPKG")

    reverse = tmp_path / "reverse.gpkg"
    gpd.GeoDataFrame(
        {"name": ["road"]},
        geometry=[LineString([(ORIGIN_X, ORIGIN_Y), (ORIGIN_X + EXTENT, ORIGIN_Y)])], crs=CRS,
    ).to_file(reverse, layer="roads", driver="GPKG")
    boundary_frame().to_file(reverse, layer="area", driver="GPKG")

    partition = partition_frame()
    first = resolve_figure_boundary(boundary_path=forward, partition=partition)
    second = resolve_figure_boundary(boundary_path=reverse, partition=partition)
    assert list(first.total_bounds) == list(second.total_bounds)


def test_a_layer_argument_is_rejected_for_single_layer_formats(tmp_path: Path) -> None:
    path = write_boundary(tmp_path / "plain.geojson")
    with pytest.raises(BoundaryContractError, match="not valid for"):
        resolve_figure_boundary(
            boundary_path=path, partition=partition_frame(), layer="anything",
        )


# ---------------------------------------------------------------------------
# Group E — CRS
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["nocrs.gpkg", "nocrs.shp"])
def test_a_boundary_without_a_crs_is_refused_rather_than_assumed(
    tmp_path: Path, name: str
) -> None:
    """E: an undeclared CRS is refused, never assumed to be the partition's."""
    path = tmp_path / name
    frame = gpd.GeoDataFrame({"name": ["a"]}, geometry=[study_area()], crs=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        frame.to_file(path)
    assert gpd.read_file(path).crs is None
    with pytest.raises(BoundaryContractError, match="no CRS"):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


def test_a_boundary_whose_declared_crs_cannot_hold_its_coordinates_is_refused(
    tmp_path: Path,
) -> None:
    """E: GeoJSON is CRS84 by definition, so projected metres inside one are wrong.

    Reprojecting metre coordinates read as degrees pushes the boundary off the
    globe. That must be refused, not drawn as an empty or infinite frame.
    """
    path = tmp_path / "metres_as_degrees.geojson"
    frame = gpd.GeoDataFrame({"name": ["a"]}, geometry=[study_area()], crs=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        frame.to_file(path)
    assert str(gpd.read_file(path).crs) == OTHER_CRS
    with pytest.raises(BoundaryContractError, match="not finite"):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame())


def test_a_partition_without_a_crs_is_refused(tmp_path: Path) -> None:
    path = write_boundary(tmp_path / "b.gpkg")
    with pytest.raises(BoundaryContractError, match="partition has no CRS"):
        resolve_figure_boundary(boundary_path=path, partition=partition_frame(crs=None))


def test_a_geographic_boundary_is_reprojected_onto_a_projected_partition(tmp_path: Path) -> None:
    """E: EPSG:4326 degrees are never mixed with projected metres."""
    projected = boundary_frame()
    geographic = projected.to_crs(OTHER_CRS)
    path = tmp_path / "geographic.geojson"
    geographic.to_file(path)
    assert max(abs(value) for value in geographic.total_bounds) < 1_000

    resolved = resolve_figure_boundary(boundary_path=path, partition=partition_frame())

    assert str(resolved.crs) == CRS
    for actual, expected in zip(resolved.total_bounds, projected.total_bounds):
        assert abs(float(actual) - float(expected)) < 1.0


def test_a_projected_boundary_is_reprojected_onto_a_geographic_partition(tmp_path: Path) -> None:
    path = write_boundary(tmp_path / "projected.gpkg")
    partition = partition_frame().to_crs(OTHER_CRS)
    with warnings.catch_warnings():
        # The containment check measures length in the partition's own CRS and
        # compares a ratio, so geopandas' "length in degrees" advisory is noise
        # here; project partitions are EPSG:32650.
        warnings.filterwarnings("ignore", "Geometry is in a geographic CRS", UserWarning)
        resolved = resolve_figure_boundary(boundary_path=path, partition=partition)
    assert str(resolved.crs) == OTHER_CRS
    assert max(abs(float(value)) for value in resolved.total_bounds) < 1_000


def test_a_boundary_in_a_disjoint_coordinate_system_fails_the_spatial_check(tmp_path: Path) -> None:
    """E: matching numbers in the wrong CRS must not pass as agreement."""
    path = tmp_path / "raw_degrees.geojson"
    gpd.GeoDataFrame(
        {"name": ["a"]},
        geometry=[Polygon([(116.0, 39.0), (116.5, 39.0), (116.5, 39.5), (116.0, 39.5)])],
        crs=OTHER_CRS,
    ).to_file(path)
    # Partition metres are ~440 000; the reprojected boundary lands elsewhere.
    with pytest.raises(BoundaryContractError, match="does not intersect|falls outside"):
        resolve_figure_boundary(
            boundary_path=path, partition=partition_frame(inset=0.45),
        )


def test_non_finite_bounds_are_refused() -> None:
    empty = gpd.GeoDataFrame({"n": []}, geometry=[], crs=CRS)
    with pytest.raises(BoundaryContractError, match="not finite"):
        check_spatial_consistency(empty, partition_frame(), source="s")


# ---------------------------------------------------------------------------
# Group F — spatial consistency
# ---------------------------------------------------------------------------

def test_a_partition_fully_inside_the_boundary_is_accepted() -> None:
    check_spatial_consistency(boundary_frame(), partition_frame(), source="s")


def test_a_partition_entirely_outside_the_boundary_is_refused() -> None:
    far = gpd.GeoDataFrame(
        {"n": ["a"]}, geometry=[study_area(scale=0.2, shift=500_000)], crs=CRS,
    )
    with pytest.raises(BoundaryContractError, match="does not intersect"):
        check_spatial_consistency(far, partition_frame(), source="s")


def test_a_boundary_from_a_different_city_is_refused() -> None:
    other_city = gpd.GeoDataFrame(
        {"n": ["a"]}, geometry=[study_area(scale=10, shift=2_000_000)], crs=CRS,
    )
    with pytest.raises(BoundaryContractError, match="does not intersect"):
        check_spatial_consistency(other_city, partition_frame(), source="s")


def test_a_boundary_covering_only_part_of_the_partition_is_refused() -> None:
    """F: overlapping is not enough; most of the partition must be covered."""
    half = gpd.GeoDataFrame(
        {"n": ["a"]},
        geometry=[Polygon([
            (ORIGIN_X, ORIGIN_Y), (ORIGIN_X + EXTENT, ORIGIN_Y),
            (ORIGIN_X + EXTENT, ORIGIN_Y + EXTENT * 0.5), (ORIGIN_X, ORIGIN_Y + EXTENT * 0.5),
        ])], crs=CRS,
    )
    with pytest.raises(BoundaryContractError, match="falls outside"):
        check_spatial_consistency(half, partition_frame(), source="s")


def test_an_extreme_extent_mismatch_still_intersecting_is_reported_by_fraction() -> None:
    sliver = gpd.GeoDataFrame(
        {"n": ["a"]},
        geometry=[Polygon([
            (ORIGIN_X + EXTENT * 0.24, ORIGIN_Y + EXTENT * 0.24),
            (ORIGIN_X + EXTENT * 0.26, ORIGIN_Y + EXTENT * 0.24),
            (ORIGIN_X + EXTENT * 0.26, ORIGIN_Y + EXTENT * 0.26),
            (ORIGIN_X + EXTENT * 0.24, ORIGIN_Y + EXTENT * 0.26),
        ])], crs=CRS,
    )
    with pytest.raises(BoundaryContractError, match="falls outside"):
        check_spatial_consistency(sliver, partition_frame(), source="s")


def test_a_boundary_clipping_slightly_inside_the_tolerance_is_accepted() -> None:
    """F: reprojection and edge clipping move geometry a little; that is fine."""
    partition = partition_frame()
    span = float(partition.geometry.length.sum())
    # Trim about 1% of the partition length, below the 2% tolerance.
    top = ORIGIN_Y + EXTENT * 0.75 - (span * 0.01) / len(partition)
    tight = gpd.GeoDataFrame(
        {"n": ["a"]},
        geometry=[Polygon([
            (ORIGIN_X, ORIGIN_Y), (ORIGIN_X + EXTENT, ORIGIN_Y),
            (ORIGIN_X + EXTENT, top), (ORIGIN_X, top),
        ])], crs=CRS,
    )
    check_spatial_consistency(tight, partition, source="s")
    assert CONTAINMENT_TOLERANCE == 0.02


def test_the_partition_geometry_is_never_modified_by_the_check() -> None:
    partition = partition_frame()
    before = [geometry.wkt for geometry in partition.geometry]
    check_spatial_consistency(boundary_frame(), partition, source="s")
    assert [geometry.wkt for geometry in partition.geometry] == before


# ---------------------------------------------------------------------------
# Group G — no partial output
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
@pytest.mark.parametrize(
    "arguments, reason",
    [
        (("--boundary", "{missing}"), "does not exist"),
        (("--boundary", "{wrong}"), "does not intersect"),
        (("--boundary", "{unsupported}"), "unsupported boundary format"),
    ],
    ids=["missing", "wrong-study-area", "unsupported-suffix"],
)
def test_every_refusal_leaves_no_figure_behind(
    tmp_path: Path, script: str, arguments: tuple[str, ...], reason: str,
) -> None:
    """G: refusal happens before the output directory is created."""
    correct = write_boundary(tmp_path / "correct.gpkg")
    run = build_run(tmp_path, boundary=correct)
    substitutions = {
        "missing": str(tmp_path / "absent.gpkg"),
        "wrong": str(write_boundary(tmp_path / "wrong.gpkg", study_area(shift=900_000))),
        "unsupported": str(tmp_path / "b.gpickle"),
    }
    Path(substitutions["unsupported"]).write_bytes(b"never opened")
    resolved = tuple(value.format(**substitutions) for value in arguments)

    output_dir = tmp_path / "figures"
    result = run_cli(script, run, output_dir, *resolved)

    assert result.returncode != 0
    assert reason in result.stderr
    assert "Traceback" not in result.stderr
    assert_no_output(output_dir)


@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
def test_an_ambiguous_layer_produces_no_output_and_lists_candidates(
    tmp_path: Path, script: str,
) -> None:
    ambiguous = _two_layer_boundary(tmp_path, "ambiguous.gpkg")
    run = build_run(tmp_path, boundary=ambiguous)
    output_dir = tmp_path / "figures"

    result = run_cli(script, run, output_dir, "--boundary", str(ambiguous))

    assert result.returncode != 0
    assert "correct_area" in result.stderr and "another_area" in result.stderr
    assert "Traceback" not in result.stderr
    assert_no_output(output_dir)


@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
def test_a_tampered_manifest_record_produces_no_output(tmp_path: Path, script: str) -> None:
    boundary = write_boundary(tmp_path / "b.gpkg")
    run = build_run(tmp_path, boundary=boundary)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["inputs"]["files"][MANIFEST_LOGICAL_NAME]["sha256"] = "0" * 64
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    output_dir = tmp_path / "figures"
    result = run_cli(script, run, output_dir, "--boundary-from-manifest")

    assert result.returncode != 0
    assert "sha256" in result.stderr
    assert_no_output(output_dir)


# ---------------------------------------------------------------------------
# Group H — visual regression
# ---------------------------------------------------------------------------

def rendered_axes(tmp_path: Path) -> Any:
    """Render the two-panel figure and return its axes for inspection."""
    import matplotlib.pyplot as plt

    boundary = write_boundary(tmp_path / "b.gpkg")
    run = build_run(tmp_path, boundary=boundary)
    partition = run / "partition" / "clusters" / "segment_clusters_road_poi_order_regularized_a.gpkg"
    resolved = resolve_figure_boundary(
        boundary_path=boundary, partition=gpd.read_file(partition),
    )
    best_partition_map.render_partition_order_figure(
        partition,
        run / "preparation" / "road_edges_classified.gpkg",
        resolved,
        run / "preparation" / "segment_relation_graph_road_poi_order.graph.json.gz",
        run / "preparation" / "segment_order_od_hourly.csv",
        tmp_path / "out.png",
        tmp_path / "out.pdf",
    )
    figure = plt.gcf() if not plt.get_fignums() else plt.figure(plt.get_fignums()[-1])
    return figure


def test_the_two_panel_figure_keeps_its_layers_arrow_scale_bar_and_extent(tmp_path: Path) -> None:
    """H: structural assertions, so a Matplotlib version cannot mask a change."""
    import matplotlib.pyplot as plt

    matplotlib.use("Agg")
    boundary_path = write_boundary(tmp_path / "b.gpkg")
    run = build_run(tmp_path, boundary=boundary_path)
    partition_path = (
        run / "partition" / "clusters" / "segment_clusters_road_poi_order_regularized_a.gpkg"
    )
    resolved = resolve_figure_boundary(
        boundary_path=boundary_path, partition=gpd.read_file(partition_path),
    )

    captured: dict[str, Any] = {}
    real_close = plt.close

    def capture(figure: Any = None) -> None:
        if figure is not None and "figure" not in captured:
            captured["figure"] = figure
            return
        real_close(figure)

    plt.close = capture  # type: ignore[assignment]
    try:
        best_partition_map.render_partition_order_figure(
            partition_path,
            run / "preparation" / "road_edges_classified.gpkg",
            resolved,
            run / "preparation" / "segment_relation_graph_road_poi_order.graph.json.gz",
            run / "preparation" / "segment_order_od_hourly.csv",
            tmp_path / "out.png",
            tmp_path / "out.pdf",
        )
    finally:
        plt.close = real_close  # type: ignore[assignment]

    figure = captured["figure"]
    axes = figure.axes
    assert len(axes) == 3, "two map panels plus the colourbar"
    assert tuple(round(value, 2) for value in figure.get_size_inches()) == (12.4, 6.2)

    left, right = axes[0], axes[1]
    # Extent is driven by the boundary, centred and padded exactly as before.
    minx, miny, maxx, maxy = (float(value) for value in resolved.total_bounds)
    span = max(maxx - minx, maxy - miny) / 0.95
    assert abs((left.get_xlim()[1] - left.get_xlim()[0]) - span) < 1e-6
    assert left.get_xlim() == right.get_xlim() and left.get_ylim() == right.get_ylim()

    # No title, panel labels, north arrow, and scale bar all still present.
    assert left.get_title() == "" and right.get_title() == ""
    texts = [text.get_text() for text in left.texts]
    assert "(a)" in texts and "N" in texts and "5 km" in texts
    assert "(b)" in [text.get_text() for text in right.texts]
    assert not left.axison and not right.axison
    assert len(left.lines) > 0 and len(right.lines) > 0
    real_close(figure)


def test_the_cluster_colour_mapping_is_deterministic_and_unchanged(tmp_path: Path) -> None:
    """H: the palette and adjacency-based assignment are untouched by R6.1."""
    clusters = partition_frame()
    graph = nx.Graph()
    graph.add_nodes_from(clusters["seg_id"])
    graph.add_edges_from([("s0", "s1"), ("s1", "s2"), ("s2", "s3")])

    first = best_partition_map.cluster_colors(clusters, graph, best_partition_map.muted_palette())
    second = best_partition_map.cluster_colors(clusters, graph, best_partition_map.muted_palette())
    assert first == second
    assert set(first) == {0, 1}
    assert first[0] != first[1], "adjacent clusters keep contrasting colours"


def test_the_same_inputs_render_byte_identical_figures(tmp_path: Path) -> None:
    """H: rendering stays deterministic for a fixed fixture."""
    boundary = write_boundary(tmp_path / "b.gpkg")
    run = build_run(tmp_path, boundary=boundary)
    digests = []
    for index in range(2):
        output_dir = tmp_path / f"figures{index}"
        result = run_cli(
            FIGURE_SCRIPTS[1], run, output_dir, "--boundary-from-manifest",
        )
        assert result.returncode == 0, result.stderr
        digests.append((output_dir / "partition_and_mean_hourly_orders.png").read_bytes())
    assert digests[0] == digests[1]


# ---------------------------------------------------------------------------
# Group I — CLI contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
def test_help_documents_the_boundary_arguments(tmp_path: Path, script: str) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / script), "--help"],
        capture_output=True, text=True, cwd=PROJECT_ROOT, env=environment,
    )
    assert result.returncode == 0
    for expected in ("--boundary", "--boundary-from-manifest", "--boundary-layer",
                     "Polygon/MultiPolygon", "CRS", ".gpkg"):
        assert expected in result.stdout, f"{script} --help omits {expected}"


@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
def test_omitting_the_boundary_is_a_usage_error(tmp_path: Path, script: str) -> None:
    """I: there is no default, so the argument parser refuses immediately."""
    boundary = write_boundary(tmp_path / "b.gpkg")
    run = build_run(tmp_path, boundary=boundary)
    output_dir = tmp_path / "figures"

    result = run_cli(script, run, output_dir)

    assert result.returncode != 0
    assert "--boundary" in result.stderr
    assert_no_output(output_dir)


@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
def test_the_two_bindings_are_mutually_exclusive(tmp_path: Path, script: str) -> None:
    boundary = write_boundary(tmp_path / "b.gpkg")
    run = build_run(tmp_path, boundary=boundary)
    output_dir = tmp_path / "figures"

    result = run_cli(
        script, run, output_dir, "--boundary", str(boundary), "--boundary-from-manifest",
    )

    assert result.returncode != 0
    assert "not allowed with" in result.stderr
    assert_no_output(output_dir)


@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
@pytest.mark.parametrize("binding", ["explicit", "manifest"])
def test_a_correct_boundary_renders_through_either_binding(
    tmp_path: Path, script: str, binding: str,
) -> None:
    boundary = write_boundary(tmp_path / "b.gpkg")
    run = build_run(tmp_path, boundary=boundary)
    output_dir = tmp_path / f"figures_{binding}"
    arguments = (
        ("--boundary", str(boundary)) if binding == "explicit" else ("--boundary-from-manifest",)
    )

    result = run_cli(script, run, output_dir, *arguments)

    assert result.returncode == 0, result.stderr
    assert (output_dir / "partition_and_mean_hourly_orders.png").is_file()


@pytest.mark.parametrize("script", FIGURE_SCRIPTS)
def test_an_explicit_layer_is_accepted_by_the_cli(tmp_path: Path, script: str) -> None:
    ambiguous = _two_layer_boundary(tmp_path, "layers.gpkg")
    run = build_run(tmp_path, boundary=ambiguous)
    output_dir = tmp_path / "figures"

    result = run_cli(
        script, run, output_dir, "--boundary", str(ambiguous),
        "--boundary-layer", "correct_area",
    )

    assert result.returncode == 0, result.stderr
    assert (output_dir / "partition_and_mean_hourly_orders.png").is_file()


def test_both_entrypoints_share_one_boundary_argument_definition() -> None:
    """I: the module entrypoint and the installed script cannot drift apart."""
    parsers = []
    for _ in range(2):
        parser = argparse.ArgumentParser()
        add_boundary_arguments(parser)
        parsers.append({action.dest for action in parser._actions})
    assert parsers[0] == parsers[1]
    assert {"boundary", "boundary_from_manifest", "boundary_layer"} <= parsers[0]


def test_the_manifest_binding_selects_the_boundary_recorded_for_that_run(tmp_path: Path) -> None:
    """I: two runs with different study areas each get their own boundary."""
    near = write_boundary(tmp_path / "near.gpkg")
    far_geometry = study_area(shift=300_000)
    far = write_boundary(tmp_path / "far.gpkg", far_geometry)

    near_run = build_run(tmp_path / "a", boundary=near)
    far_run = build_run(
        tmp_path / "b", boundary=far,
        partition=gpd.GeoDataFrame(
            {"seg_id": ["s0"], "cluster_id": [0]},
            geometry=[LineString([
                (ORIGIN_X + 300_000 + EXTENT * 0.3, ORIGIN_Y + 300_000 + EXTENT * 0.3),
                (ORIGIN_X + 300_000 + EXTENT * 0.7, ORIGIN_Y + 300_000 + EXTENT * 0.7),
            ])], crs=CRS,
        ),
    )

    for run, expected in ((near_run, near), (far_run, far)):
        manifest = load_run_manifest(run)
        assert boundary_path_from_manifest(manifest, run_dir=run) == expected

    # The near run must never resolve the far boundary, which is the AUD-007 bug.
    near_manifest = load_run_manifest(near_run)
    partition = gpd.read_file(
        near_run / "partition" / "clusters" / "segment_clusters_road_poi_order_regularized_a.gpkg"
    )
    resolved = resolve_boundary(
        namespace(boundary_from_manifest=True), run=near_run,
        manifest=near_manifest, partition=partition,
    )
    assert list(resolved.total_bounds) == list(boundary_frame().total_bounds)


def test_a_missing_run_manifest_is_reported_without_a_traceback(tmp_path: Path) -> None:
    with pytest.raises(BoundaryContractError, match="run manifest not found"):
        load_run_manifest(tmp_path / "absent-run")
