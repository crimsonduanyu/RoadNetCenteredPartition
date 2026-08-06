from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString
import yaml

from roadnet_partition.io.manifests import (
    RUN_MARKER,
    STAGE_RESULT_FILENAME,
    SUCCESS_MARKER,
    load_manifest,
    sha256_file,
)
from roadnet_partition.io.safe_graph import ARTIFACT_SUFFIX, write_safe_graph


DEMAND_FILES = {
    "cluster_index.csv",
    "orders_region_assigned.csv.gz",
    "cluster_od_15min.csv",
    "od_tensor_15min.npz",
    "metadata.json",
    "cluster_graph_road_edges.csv",
    "cluster_graph_road_adjacency_raw.npz",
    "cluster_graph_road_adjacency_normalized.npz",
    "cluster_graph_poi_edges.csv",
    "cluster_graph_poi_adjacency_raw.npz",
    "cluster_graph_poi_adjacency_normalized.npz",
    "cluster_graph_distance_edges.csv",
    "cluster_graph_distance_adjacency_raw.npz",
    "cluster_graph_distance_adjacency_normalized.npz",
    "cluster_poi_features.csv",
    "cluster_poi_category_mapping.csv",
}
SUPPLY_FILES = {
    "supply_inservice_od.csv.gz",
    "supply_available_floor.csv.gz",
    "supply_fleet_lower_bound.csv.gz",
    "run_summary.json",
    "config_used.json",
}
TTE_FILES = {
    "cluster_network_distance.parquet",
    "cluster_representative_nodes.csv",
    "TTE_raw.parquet",
    "TTE_count.parquet",
    "TTE_support.parquet",
    "TTE_hops.parquet",
    "TTE_imputed.parquet",
}


def write_yaml(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def write_dataset(project: Path) -> Path:
    return write_yaml(project / "configs/datasets/tiny.yaml", {
        "schema_version": 1,
        "project_root": "../..",
        "scope": "tiny",
        "crs": {"projected": "EPSG:32631", "geographic": "EPSG:4326"},
        "study_area": {},
    })


def write_partition_fixture(project: Path) -> Path:
    root = project / "inputs/partition"
    root.mkdir(parents=True)
    graph = nx.Graph()
    for left, right, weight in (("a", "b", 5.0), ("b", "c", 1.0), ("c", "d", 5.0)):
        graph.add_edge(left, right, weight=weight, continuity_weight=weight, connector_weight=weight)
    graph_path = root / f"graph{ARTIFACT_SUFFIX}"
    write_safe_graph(graph, graph_path)
    segments = gpd.GeoDataFrame(
        {"seg_id": ["a", "b", "c", "d"], "length": [1.0] * 4},
        geometry=[
            LineString([(0, 0), (1, 0)]),
            LineString([(1, 0), (2, 0)]),
            LineString([(2, 0), (3, 0)]),
            LineString([(3, 0), (4, 0)]),
        ],
        crs="EPSG:3857",
    )
    segment_path = root / "segments.gpkg"
    segments.to_file(segment_path, driver="GPKG")
    baseline_path = root / "baseline.gpkg"
    segments.assign(cluster_id=[0, 0, 1, 1]).to_file(baseline_path, driver="GPKG")
    pd.DataFrame({"seg_id": ["a", "b", "c", "d"], "order_total": [1, 1, 5, 5]}).to_csv(
        root / "orders.csv", index=False,
    )
    pd.DataFrame({"seg_id": ["a", "b", "c", "d"], "poi_total": [1, 0, 0, 1]}).to_csv(
        root / "poi.csv", index=False,
    )
    pd.DataFrame({"seg_id_a": ["a"], "seg_id_b": ["b"]}).to_csv(root / "relations.csv", index=False)
    return write_yaml(project / "configs/zoning/partition.yaml", {
        "schema_version": 1,
        "dataset_config": "../datasets/tiny.yaml",
        "scope": "tiny",
        "contract": {"verify_canonical": True, "expected_partition": "../../inputs/partition/baseline.gpkg"},
        "stage1_partition": {
            "graph_variant": "road",
            "regularized": {
                "initialization": "leiden",
                "inputs": {
                    "graph": f"../../inputs/partition/graph{ARTIFACT_SUFFIX}",
                    "relation_edges": "../../inputs/partition/relations.csv",
                    "classified_edges": "../../inputs/partition/segments.gpkg",
                    "boundary": "../../inputs/partition/segments.gpkg",
                    "segment_nodes": "../../inputs/partition/segments.gpkg",
                    "poi_features": "../../inputs/partition/poi.csv",
                    "order_features": "../../inputs/partition/orders.csv",
                    "hourly_od": "../../inputs/partition/orders.csv",
                },
                "baseline_clusters": {"leiden": "../../inputs/partition/baseline.gpkg"},
                "objective": {
                    "target_clusters": 2,
                    "capacity_loss": "squared_hinge",
                    "capacity_min_ratio": 0.5,
                    "capacity_max_ratio": 1.5,
                    "lambda_g": 1.0,
                    "lambda_r": 1.0,
                    "alpha_cont": 1.0,
                    "alpha_conn": 1.0,
                    "grid": {"lambda_c": [1.0]},
                },
                "search": {
                    "max_passes": 0,
                    "min_delta": 1.0e-9,
                    "move_policy": "best_improving",
                    "enforce_connectivity": True,
                    "allow_merge_split": False,
                    "grid": {"merge_split_enabled": [False]},
                },
            },
            "outputs": {
                "run_root": "../../standalone/partition",
                "canonical_partition": "../../inputs/partition/baseline.gpkg",
            },
        },
    })


def write_demand_fixture(project: Path) -> Path:
    root = project / "inputs/demand"
    root.mkdir(parents=True)
    partition = gpd.GeoDataFrame(
        {"seg_id": ["s1", "s2"], "cluster_id": ["1", "2"], "length": [100.0, 100.0]},
        geometry=[LineString([(0.0, 0.0), (0.001, 0.0)]), LineString([(0.01, 0.0), (0.011, 0.0)])],
        crs="EPSG:4326",
    ).to_crs("EPSG:32631")
    partition.to_file(root / "partition.gpkg", driver="GPKG")
    pd.DataFrame([{"seg_id_a": "s1", "seg_id_b": "s2", "base_weight": 2.0}]).to_csv(
        root / "relations.csv", index=False,
    )
    pd.DataFrame([
        (1, 11, 0.0002, 0.0, 0.0102, 0.0, "2020-01-01 08:01:00", "2020-01-01 08:20:00"),
        (2, 11, 0.0003, 0.0, 0.0103, 0.0, "2020-01-01 08:05:00", "2020-01-01 08:25:00"),
        (3, 22, 0.0102, 0.0, 0.0002, 0.0, "2020-01-01 08:15:00", "2020-01-01 08:30:00"),
    ], columns=[
        "order_id", "driver_id", "starting_lng", "starting_lat", "dest_lng", "dest_lat",
        "departure_time", "finish_time",
    ]).to_csv(root / "orders.csv", index=False)
    pd.DataFrame([(0.0002, 0.0, "food"), (0.0102, 0.0, "office")], columns=["lon", "lat", "cat"]).to_csv(
        root / "poi.csv", index=False,
    )
    return write_yaml(project / "configs/pipelines/demand.yaml", {
        "schema_version": 1,
        "dataset_config": "../datasets/tiny.yaml",
        "scope": "tiny",
        "order_pipeline": {
            "inputs": {
                "partition_gpkg": "../../inputs/demand/partition.gpkg",
                "road_relation_edges_csv": "../../inputs/demand/relations.csv",
                "order_datasets": ["../../inputs/demand/orders.csv"],
                "poi_path": "../../inputs/demand/poi.csv",
            },
            "outputs": {"root": None},
            "keep_staging_db": False,
            "time_slot_minutes": 15,
            "order": {
                "chunksize": 2,
                "max_match_distance_m": 250,
                "start_time": "2020-01-01 00:00:00",
                "end_time": "2020-01-02 00:00:00",
                "order_id_column": "order_id",
                "driver_id_column": "driver_id",
                "pickup_lon_column": "starting_lng",
                "pickup_lat_column": "starting_lat",
                "dropoff_lon_column": "dest_lng",
                "dropoff_lat_column": "dest_lat",
                "departure_time_column": "departure_time",
                "finish_time_column": "finish_time",
            },
            "poi": {
                "lon_column": "lon",
                "lat_column": "lat",
                "category_column": "cat",
                "max_match_distance_m": 250,
                "similarity_top_k": 1,
            },
            "road_graph": {"weight_column": "base_weight"},
            "distance_graph": {"top_k": 1, "decay_distance_m": 1000.0},
            "graph_normalization": {"add_self_loops": True, "symmetric": True},
        },
    })


def write_supply_fixture(project: Path) -> Path:
    root = project / "inputs/supply"
    root.mkdir(parents=True)
    pd.DataFrame([
        (1, 101, "2020-01-01 08:00:00", "2020-01-01 08:10:00", 1, 2, "exclusive"),
        (2, 101, "2020-01-01 08:20:00", "2020-01-01 08:30:00", 2, 1, "exclusive"),
        (3, 202, "2020-01-01 08:05:00", "2020-01-01 08:25:00", 1, 2, "carpool"),
    ], columns=[
        "order_id", "driver_id", "departure_time", "finish_time",
        "origin_cluster_id", "destination_cluster_id", "service_type",
    ]).to_csv(root / "orders.csv.gz", index=False, compression="gzip")
    return write_yaml(project / "configs/pipelines/supply.yaml", {
        "schema_version": 1,
        "dataset_config": "../datasets/tiny.yaml",
        "scope": "tiny",
        "stage3_supply": {
            "orders_path": "../../inputs/supply/orders.csv.gz",
            "output_dir": "../../standalone/supply",
            "max_gap_minutes": 60,
            "tau_idle_minutes": 30,
            "carpool_merge_gap_s": 0,
            "slot_duration_min": 10,
            "n_blocks": 2,
        },
    })


def write_tte_fixture(project: Path) -> Path:
    root = project / "inputs/tte"
    root.mkdir(parents=True)
    orders = pd.DataFrame([
        ("2020-01-01 00:00:00", "2020-01-01 00:10:00", "1", "2"),
        ("2020-01-01 00:00:00", "2020-01-01 00:12:00", "2", "1"),
        ("2020-01-01 00:10:00", "2020-01-01 00:20:00", "1", "2"),
        ("2020-01-01 00:10:00", "2020-01-01 00:22:00", "2", "1"),
    ], columns=["departure_time", "finish_time", "origin_cluster_id", "destination_cluster_id"])
    orders.to_csv(root / "orders.csv.gz", index=False, compression="gzip")
    pd.DataFrame({
        "cluster_id": ["1", "2"],
        "centroid_lon": [0.0, 0.01],
        "centroid_lat": [0.0, 0.0],
    }).to_csv(root / "cluster_index.csv", index=False)
    pd.DataFrame([[0.0, 5000.0], [5000.0, 0.0]], index=["1", "2"], columns=["1", "2"]).to_parquet(
        root / "distance.parquet",
    )
    pd.DataFrame({
        "cluster_id": ["1", "2"],
        "rep_osmid": [101, 202],
        "dist_to_centroid_m": [1.0, 2.0],
    }).to_csv(root / "representatives.csv", index=False)
    return write_yaml(project / "configs/pipelines/tte.yaml", {
        "schema_version": 1,
        "dataset_config": "../datasets/tiny.yaml",
        "scope": "tiny",
        "stage4_tte": {
            "inputs": {
                "orders_path": "../../inputs/tte/orders.csv.gz",
                "cluster_index_path": "../../inputs/tte/cluster_index.csv",
                "network_distance_path": "../../inputs/tte/distance.parquet",
                "representative_nodes_path": "../../inputs/tte/representatives.csv",
            },
            "output_dir": "../../standalone/tte",
            "outputs": {
                "count_filename": "TTE_count.parquet",
                "hops_filename": "TTE_hops.parquet",
                "support_filename": "TTE_support.parquet",
            },
            "distance": {
                "matrix_filename": "cluster_network_distance.parquet",
                "representatives_filename": "cluster_representative_nodes.csv",
                "recompute": False,
            },
            "time": {
                "freq": "10min",
                "start_time": "2020-01-01 00:00:00",
                "end_time": "2020-01-01 00:10:00",
            },
            "trip_time": {"min_minutes": 3, "max_minutes": 80, "aggregation": "median"},
            "keep_place": {"min_origin_orders": 1, "min_dest_orders": 1},
            "imputation": {
                "method": "transitive",
                "max_hops": 2,
                "source_min_count": 1,
                "detour_ratio": 1.3,
                "speed_limit_kmh": [5, 120],
                "min_dist_km": 0.01,
                "window": 2,
                "outlier_std_threshold": 3,
                "use_validation": True,
            },
        },
    })


def clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return environment


def run_cli(executable: str, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, *args],
        cwd=cwd,
        env=clean_environment(),
        check=False,
        capture_output=True,
        text=True,
    )


def formal_relative_paths(run_dir: Path, stage: str) -> set[str]:
    manifest = load_manifest(run_dir)
    stage_dir = run_dir / stage
    return {
        Path(record["path"]).relative_to(stage_dir).as_posix()
        for record in manifest["stages"][stage]["outputs"].values()
    }


def assert_run_lifecycle(run_dir: Path, stage: str, expected_files: set[str]) -> None:
    marker = json.loads((run_dir / RUN_MARKER).read_text(encoding="utf-8"))
    resolved = json.loads((run_dir / "resolved_config.json").read_text(encoding="utf-8"))
    manifest = load_manifest(run_dir)
    stage_result = json.loads((run_dir / stage / STAGE_RESULT_FILENAME).read_text(encoding="utf-8"))
    success = json.loads((run_dir / stage / SUCCESS_MARKER).read_text(encoding="utf-8"))
    assert marker["run_id"] == manifest["run_id"]
    assert resolved["fingerprint"] == manifest["config"]["fingerprint"]
    assert manifest["status"] == "complete"
    assert set(manifest["stages"]) == {stage}
    record = manifest["stages"][stage]
    assert record["status"] == "complete"
    assert record["contract"]["status"] == "passed"
    assert stage_result["status"] == "complete"
    assert success["stage"] == stage
    assert set(record["outputs"]) == set(stage_result["outputs"]) == set(success["outputs"])
    assert formal_relative_paths(run_dir, stage) == expected_files
    for name, output in record["outputs"].items():
        path = Path(output["path"])
        assert path.is_file() and path.is_relative_to(run_dir / stage)
        assert output["sha256"] == sha256_file(path)
        assert output == stage_result["outputs"][name] == success["outputs"][name]
    for input_record in manifest["inputs"]["files"].values():
        assert len(input_record["sha256"]) == 64


def test_four_public_stage_commands_run_real_tiny_fixtures_outside_repository(tmp_path: Path) -> None:
    executable = shutil.which("roadnet-partition")
    assert executable is not None
    project = tmp_path / "project"
    write_dataset(project)
    configs = {
        "partition": write_partition_fixture(project),
        "demand": write_demand_fixture(project),
        "supply": write_supply_fixture(project),
        "tte": write_tte_fixture(project),
    }
    outside_cwd = tmp_path / "outside-cwd"
    outside_cwd.mkdir()
    expected = {
        "partition": {
            "resolved_config.yaml",
            "tables/run_manifest.csv",
            "tables/objective_trace.csv",
            "clusters/segment_clusters_road_regularized_leiden_lc1p0_lr1p0.gpkg",
            "clusters/segment_clusters_road_regularized_leiden_lc1p0_lr1p0.csv",
        },
        "demand": DEMAND_FILES,
        "supply": SUPPLY_FILES,
        "tte": TTE_FILES,
    }

    for stage, config in configs.items():
        run_dir = tmp_path / "runs" / stage
        command = [executable, stage, "--config", str(config.resolve()), "--run-dir", str(run_dir)]
        if stage == "supply":
            command.extend(["--n-blocks", "3"])
        completed = run_cli(executable, outside_cwd, *command[1:])
        assert completed.returncode == 0, completed.stderr
        assert f"{stage}: complete" in completed.stdout
        assert "Traceback" not in completed.stderr
        assert_run_lifecycle(run_dir, stage, expected[stage])

    supply_manifest = load_manifest(tmp_path / "runs/supply")
    assert supply_manifest["config"]["resolved"]["stage3_supply"]["n_blocks"] == 3
    assert supply_manifest["config"]["resolved"]["_resolved"]["overrides"] == {"n_blocks": 3}
    assert not (project / "data/processed").exists()


def test_cli_exit_codes_for_config_contract_resume_and_stage_failures(tmp_path: Path) -> None:
    executable = shutil.which("roadnet-partition")
    assert executable is not None
    project = tmp_path / "project"
    write_dataset(project)
    config_path = write_supply_fixture(project)
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    missing = run_cli(
        executable, cwd, "supply", "--config", str(project / "missing.yaml"),
        "--run-dir", str(tmp_path / "missing-run"),
    )
    assert missing.returncode == 2
    assert "configuration error" in missing.stderr

    run_dir = tmp_path / "resume-run"
    first = run_cli(executable, cwd, "supply", "--config", str(config_path), "--run-dir", str(run_dir))
    assert first.returncode == 0
    conflict = run_cli(executable, cwd, "supply", "--config", str(config_path), "--run-dir", str(run_dir))
    assert conflict.returncode == 4
    resumed = run_cli(
        executable, cwd, "supply", "--config", str(config_path),
        "--run-dir", str(run_dir), "--resume",
    )
    assert resumed.returncode == 0
    assert "supply: reused" in resumed.stdout
    (run_dir / "supply/config_used.json").write_text("{}\n", encoding="utf-8")
    damaged = run_cli(
        executable, cwd, "supply", "--config", str(config_path),
        "--run-dir", str(run_dir), "--resume",
    )
    assert damaged.returncode == 4

    cluster_index = project / "inputs/supply/wrong_cluster_index.csv"
    pd.DataFrame({"cluster_id": [999]}).to_csv(cluster_index, index=False)
    contract_raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    contract_raw["stage3_supply"]["cluster_index_path"] = "../../inputs/supply/wrong_cluster_index.csv"
    contract_config = write_yaml(project / "configs/pipelines/supply-contract-fail.yaml", contract_raw)
    contract_run = tmp_path / "contract-run"
    contract = run_cli(
        executable, cwd, "supply", "--config", str(contract_config),
        "--run-dir", str(contract_run),
    )
    assert contract.returncode == 3
    assert "supply contract failed" in contract.stderr
    assert load_manifest(contract_run)["stages"]["supply"]["status"] == "failed"
    assert not (contract_run / "supply" / SUCCESS_MARKER).exists()

    bad_orders = project / "inputs/supply/bad_orders.csv.gz"
    pd.DataFrame({
        "origin_cluster_id": [1],
        "destination_cluster_id": [2],
    }).to_csv(bad_orders, index=False, compression="gzip")
    stage_raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    stage_raw["stage3_supply"]["orders_path"] = "../../inputs/supply/bad_orders.csv.gz"
    stage_config = write_yaml(project / "configs/pipelines/supply-stage-fail.yaml", stage_raw)
    stage_run = tmp_path / "stage-run"
    failed = run_cli(
        executable, cwd, "supply", "--config", str(stage_config),
        "--run-dir", str(stage_run),
    )
    assert failed.returncode != 0
    assert "Traceback" in failed.stderr
    assert load_manifest(stage_run)["stages"]["supply"]["status"] == "failed"
