from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

from roadnet_partition.downstream import tte
from roadnet_partition.downstream.tte_contracts import validate_tte_outputs


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_PATH = PROJECT_ROOT / "src/lib/tte_dataset.py"
NEW_PATH = PROJECT_ROOT / "src/roadnet_partition/downstream/tte.py"
FORMAL_FILES = {
    "cluster_network_distance.parquet",
    "cluster_representative_nodes.csv",
    "TTE_raw.parquet",
    "TTE_count.parquet",
    "TTE_support.parquet",
    "TTE_hops.parquet",
    "TTE_imputed.parquet",
}


def load_legacy_tte():
    spec = importlib.util.spec_from_file_location("legacy_tte_for_equivalence", LEGACY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_tiny_inputs(root: Path) -> tuple[Path, Path, list[str]]:
    clusters = ["2", "10", "A", "B", "isolated"]
    positions = {"2": 0.0, "10": 5.0, "A": 10.0, "B": 15.0}
    distance = np.full((len(clusters), len(clusters)), np.inf, dtype="float64")
    np.fill_diagonal(distance, 0.0)
    for i, origin in enumerate(clusters[:-1]):
        for j, destination in enumerate(clusters[:-1]):
            distance[i, j] = abs(positions[origin] - positions[destination]) * 1000.0
    matrix = pd.DataFrame(distance, index=clusters, columns=clusters)

    rows = []
    slots = pd.date_range("2020-01-01 00:00:00", periods=6, freq="10min")
    for slot in slots:
        rows.extend([
            (slot, slot + pd.Timedelta(minutes=10), "2", "10"),
            (slot, slot + pd.Timedelta(minutes=10), "10", "A"),
            (slot, slot + pd.Timedelta(minutes=10), "A", "B"),
            (slot, slot + pd.Timedelta(minutes=10), "B", "A"),
            (slot, slot + pd.Timedelta(minutes=5), "2", "2"),
            (slot, slot + pd.Timedelta(minutes=5), "isolated", "isolated"),
        ])
    rows.extend([
        (slots[0], slots[0] + pd.Timedelta(minutes=8), "2", "10"),
        (slots[0], slots[0], "A", "2"),
        (slots[1], slots[1] - pd.Timedelta(minutes=1), "A", "2"),
        (slots[2], None, "A", "2"),
    ])
    orders = pd.DataFrame(
        rows,
        columns=[tte.DEPARTURE_COL, tte.FINISH_COL, tte.ORIGIN_COL, tte.DESTINATION_COL],
    )
    orders[tte.DEPARTURE_COL] = pd.to_datetime(orders[tte.DEPARTURE_COL]).dt.strftime("%Y-%m-%d %H:%M:%S")
    orders[tte.FINISH_COL] = pd.to_datetime(orders[tte.FINISH_COL]).dt.strftime("%Y-%m-%d %H:%M:%S")
    orders_path = root / "orders_region_assigned.csv.gz"
    orders.to_csv(orders_path, index=False, compression="gzip")
    cluster_index_path = root / "cluster_index.csv"
    pd.DataFrame({
        "cluster_id": clusters,
        "centroid_lon": np.arange(len(clusters), dtype=float),
        "centroid_lat": np.arange(len(clusters), dtype=float),
    }).to_csv(cluster_index_path, index=False)
    for output in [root / "legacy", root / "new"]:
        output.mkdir()
        matrix.to_parquet(output / "cluster_network_distance.parquet")
        pd.DataFrame({
            "cluster_id": clusters,
            "rep_osmid": [102, 110, 201, 202, 999],
            "dist_to_centroid_m": [1.0, 2.0, 3.0, 4.0, 5.0],
        }).to_csv(output / "cluster_representative_nodes.csv", index=False)
    return orders_path, cluster_index_path, clusters


def tiny_config(orders_path: Path, cluster_index_path: Path, output_dir: Path) -> dict:
    return {
        "study_area": {"active": "tiny"},
        "crs": {"projected": "EPSG:32650", "geographic": "EPSG:4326"},
        "stage4_tte": {
            "inputs": {
                "orders_path": str(orders_path),
                "cluster_index_path": str(cluster_index_path),
            },
            "output_dir": str(output_dir),
            "distance": {
                "matrix_filename": "cluster_network_distance.parquet",
                "representatives_filename": "cluster_representative_nodes.csv",
                "recompute": False,
            },
            "time": {
                "freq": "10min",
                "start_time": "2020-01-01 00:00:00",
                "end_time": "2020-01-01 00:50:00",
            },
            "trip_time": {"min_minutes": 3, "max_minutes": 80, "aggregation": "median"},
            "keep_place": {"min_origin_orders": 1, "min_dest_orders": 1},
            "imputation": {
                "method": "transitive",
                "max_hops": 3,
                "source_min_count": 1,
                "detour_ratio": 1.3,
                "speed_limit_kmh": [5, 120],
                "min_dist_km": 0.01,
                "window": 6,
                "outlier_std_threshold": 3,
                "use_validation": True,
            },
        },
    }


def test_mechanical_tte_definitions_match_legacy_ast() -> None:
    legacy_tree = ast.parse(LEGACY_PATH.read_text(encoding="utf-8"))
    new_tree = ast.parse(NEW_PATH.read_text(encoding="utf-8"))
    legacy = {node.name: node for node in legacy_tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    new = {node.name: node for node in new_tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    names = {
        "SpatialPruner", "parse_columns", "get_transitive_data", "calculate_transitive_time",
        "vectorize_transitive_impute", "validate_estimates", "run_imputation_pipeline",
        "compute_keep_clusters", "build_od_columns", "build_tte_raw", "_imputation_config",
        "_nan_ratio", "resolve_output_dir", "run_from_config", "main",
    }
    for name in names - {"run_from_config"}:
        assert ast.dump(legacy[name], include_attributes=False) == ast.dump(new[name], include_attributes=False)
    legacy_run = legacy["run_from_config"]
    new_run = new["run_from_config"]
    legacy_run.body = [node for node in legacy_run.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    new_run.body = [node for node in new_run.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    assert ast.dump(legacy_run, include_attributes=False) == ast.dump(new_run, include_attributes=False)


def test_legacy_and_new_tiny_tte_outputs_are_exact(tmp_path: Path) -> None:
    legacy = load_legacy_tte()
    orders_path, cluster_index_path, clusters = write_tiny_inputs(tmp_path)
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"

    legacy_summary = legacy.run_from_config(tiny_config(orders_path, cluster_index_path, legacy_dir))
    new_summary = tte.run_from_config(tiny_config(orders_path, cluster_index_path, new_dir))

    for summary in (legacy_summary, new_summary):
        summary.pop("output_dir")
        summary["count_path"] = Path(summary["count_path"]).name
    assert legacy_summary == new_summary
    assert {path.name for path in legacy_dir.iterdir()} == FORMAL_FILES
    assert {path.name for path in new_dir.iterdir()} == FORMAL_FILES
    for filename in FORMAL_FILES - {"cluster_representative_nodes.csv"}:
        pd.testing.assert_frame_equal(
            pd.read_parquet(legacy_dir / filename),
            pd.read_parquet(new_dir / filename),
            check_exact=True,
        )
    pd.testing.assert_frame_equal(
        pd.read_csv(legacy_dir / "cluster_representative_nodes.csv"),
        pd.read_csv(new_dir / "cluster_representative_nodes.csv"),
        check_exact=True,
    )

    expected_time = pd.date_range("2020-01-01 00:00:00", periods=6, freq="10min")
    for output in [legacy_dir, new_dir]:
        result = validate_tte_outputs(
            output,
            expected_cluster_ids=clusters,
            expected_time_index=expected_time,
            raw_range=(3, 80),
            max_hops=3,
            batch_size=1,
        )
        assert result["observed_cells"] > 0
        assert result["inferred_cells"] > 0
        assert result["missing_cells"] > 0

    raw = pd.read_parquet(new_dir / "TTE_raw.parquet")
    count = pd.read_parquet(new_dir / "TTE_count.parquet")
    hops = pd.read_parquet(new_dir / "TTE_hops.parquet")
    imputed = pd.read_parquet(new_dir / "TTE_imputed.parquet")
    assert list(raw.columns[:5]) == [f"2->{cluster}" for cluster in clusters]
    assert count.loc[expected_time[0], "2->10"] == 2
    assert raw.loc[expected_time[0], "2->10"] == 9.0
    assert count["A->2"].sum() == 0
    assert raw["2->2"].notna().all()
    assert raw["A->A"].isna().all() and imputed["A->A"].isna().all()
    assert hops["2->A"].max() >= 1
    assert hops["2->B"].max() >= 2
    assert imputed["2->isolated"].isna().all()
