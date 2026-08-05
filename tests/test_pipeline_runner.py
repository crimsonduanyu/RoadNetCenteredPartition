from __future__ import annotations

from copy import deepcopy
import json
import io
from pathlib import Path
import pickle
import shutil
import subprocess

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import pytest
import yaml

from roadnet_partition.config import ConfigError
from roadnet_partition.downstream.supply import load_orders
from roadnet_partition.io.manifests import SUCCESS_MARKER, load_manifest, sha256_file
from roadnet_partition.io import manifests as manifests_module
from roadnet_partition.io.manifests import MANIFEST_FILENAME, atomic_write_json, validate_manifest
from roadnet_partition.pipeline import runner as runner_module
from roadnet_partition.pipeline import stages as stages_module
from roadnet_partition.pipeline.runner import resolve_pipeline_config, run_pipeline
from roadnet_partition.pipeline.stages import (
    ResumeConflictError, RunConflictError, STAGE_ORDER, StageContractError,
)
from roadnet_partition.io.paths import UnsafePathError
from roadnet_partition.pipeline.worker import _load_config, _load_request
from test_phase6a_cli_e2e import (
    clean_environment,
    write_dataset,
    write_demand_fixture,
    write_supply_fixture,
    write_tte_fixture,
    write_yaml,
)


def write_full_fixture(project: Path) -> Path:
    write_dataset(project)
    demand_path = write_demand_fixture(project)
    raw_orders_path = project / "inputs/demand/orders.csv"
    raw_orders = pd.read_csv(raw_orders_path)
    raw_orders["order_id"] = pd.Series(["o-1", "000123", pd.NA], dtype="string")
    raw_orders["driver_id"] = pd.Series(["driver-A", "driver-A", "司机甲"], dtype="string")
    raw_orders.to_csv(raw_orders_path, index=False)
    write_supply_fixture(project)
    tte_path = write_tte_fixture(project)

    demand_partition = gpd.read_file(project / "inputs/demand/partition.gpkg")
    root = project / "inputs/partition"
    root.mkdir(parents=True)
    graph = nx.Graph()
    graph.add_edge("s1", "s2", weight=1.0, continuity_weight=1.0, connector_weight=1.0)
    with (root / "graph.gpickle").open("wb") as handle:
        pickle.dump(graph, handle)
    demand_partition.to_file(root / "segments.gpkg", driver="GPKG")
    demand_partition.to_file(root / "baseline.gpkg", driver="GPKG")
    pd.DataFrame({"seg_id": ["s1", "s2"], "order_total": [1, 1]}).to_csv(root / "orders.csv", index=False)
    pd.DataFrame({"seg_id": ["s1", "s2"], "poi_total": [1, 1]}).to_csv(root / "poi.csv", index=False)
    pd.DataFrame({"seg_id_a": ["s1"], "seg_id_b": ["s2"]}).to_csv(root / "relations.csv", index=False)
    partition_path = write_yaml(project / "configs/zoning/partition.yaml", {
        "schema_version": 1,
        "dataset_config": "../datasets/tiny.yaml",
        "scope": "tiny",
        "contract": {"verify_canonical": True, "expected_partition": "../../inputs/partition/baseline.gpkg"},
        "stage1_partition": {
            "graph_variant": "road",
            "regularized": {
                "initialization": "leiden",
                "inputs": {
                    "graph": "../../inputs/partition/graph.gpickle",
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
                    "target_clusters": 2, "capacity_loss": "squared_hinge",
                    "capacity_min_ratio": 0.5, "capacity_max_ratio": 1.5,
                    "lambda_g": 1.0, "lambda_r": 1.0, "alpha_cont": 1.0, "alpha_conn": 1.0,
                    "grid": {"lambda_c": [1.0]},
                },
                "search": {
                    "max_passes": 0, "min_delta": 1.0e-9, "move_policy": "best_improving",
                    "enforce_connectivity": True, "allow_merge_split": False,
                    "grid": {"merge_split_enabled": [False]},
                },
            },
            "outputs": {
                "run_root": "../../standalone/partition",
                "canonical_partition": "../../inputs/partition/baseline.gpkg",
            },
        },
    })
    tte = yaml.safe_load(tte_path.read_text(encoding="utf-8"))
    tte["stage4_tte"]["time"] = {
        "freq": "10min", "start_time": "2020-01-01 08:00:00", "end_time": "2020-01-01 08:30:00",
    }
    write_yaml(tte_path, tte)
    pd.DataFrame([[0.0, 5000.0], [5000.0, 0.0]], index=["0", "1"], columns=["0", "1"]).to_parquet(
        project / "inputs/tte/distance.parquet",
    )
    pd.DataFrame({
        "cluster_id": ["0", "1"], "rep_osmid": [101, 202], "dist_to_centroid_m": [1.0, 2.0],
    }).to_csv(project / "inputs/tte/representatives.csv", index=False)
    return write_yaml(project / "configs/pipelines/full.yaml", {
        "schema_version": 1,
        "project_root": "../..",
        "scope": "tiny",
        "run": {"root": "../../runs", "isolate_stages": True},
        "stages": {
            "partition": {"config": "../zoning/partition.yaml", "required": True},
            "demand": {"config": demand_path.name, "required": True},
            "supply": {"config": "supply.yaml", "required": True},
            "tte": {"config": "tte.yaml", "required": True},
        },
    })


def test_pipeline_resolver_and_ranges(tmp_path: Path) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    assert tuple(config.stages) == STAGE_ORDER
    first = run_pipeline(config, run_dir=tmp_path / "partition-only", to_stage="partition", isolate_stages=False)
    manifest = load_manifest(first.run_dir)
    assert [manifest["stages"][stage]["status"] for stage in STAGE_ORDER] == [
        "complete", "not_started", "not_started", "not_started",
    ]
    assert manifest["pipeline"]["completed_through"] == "partition"
    assert manifest["pipeline"]["all_required_stages_complete"] is False
    with pytest.raises(RunConflictError, match="must start from partition"):
        run_pipeline(config, run_dir=tmp_path / "new-supply", from_stage="supply", isolate_stages=False)
    with pytest.raises(ValueError, match="later than"):
        run_pipeline(config, run_dir=tmp_path / "bad-range", from_stage="tte", to_stage="demand")
    with pytest.raises(UnsafePathError):
        run_pipeline(config, run_dir=config.project_root / "data/processed/tiny/run")


@pytest.mark.parametrize("value", ["", "../victim", r"C:\victim", r"\\server\share"])
def test_pipeline_scope_is_validated_before_stage_resolution(tmp_path: Path, value: str) -> None:
    path = write_full_fixture(tmp_path / "project")
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["scope"] = value
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ConfigError, match="pipeline scope"):
        resolve_pipeline_config(path)


def test_pipeline_scope_mismatch_fails_during_resolution(tmp_path: Path) -> None:
    path = write_full_fixture(tmp_path / "project")
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["scope"] = "other"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(ConfigError, match="pipeline scope conflicts with partition scope"):
        resolve_pipeline_config(path)


def test_pipeline_accepts_matching_unicode_dataset_and_stage_scope(tmp_path: Path) -> None:
    path = write_full_fixture(tmp_path / "project")
    for config_path in (tmp_path / "project/configs").rglob("*.yaml"):
        values = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if "scope" in values:
            values["scope"] = "北京五环"
            config_path.write_text(yaml.safe_dump(values, allow_unicode=True), encoding="utf-8")

    assert resolve_pipeline_config(path).scope == "北京五环"


def test_direct_and_isolated_full_pipeline_are_contract_equivalent(tmp_path: Path) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    direct = run_pipeline(config, run_dir=tmp_path / "direct", isolate_stages=False)
    isolated = run_pipeline(config, run_dir=tmp_path / "isolated", isolate_stages=True)
    for result in (direct, isolated):
        manifest = load_manifest(result.run_dir)
        assert manifest["run_kind"] == "pipeline"
        assert manifest["pipeline"]["all_required_stages_complete"] is True
        assert all(manifest["stages"][stage]["status"] == "complete" for stage in STAGE_ORDER)
        assert all((result.run_dir / stage / SUCCESS_MARKER).is_file() for stage in STAGE_ORDER)
        assert all((result.run_dir / "resolved_configs" / f"{stage}.yaml").is_file() for stage in STAGE_ORDER)
        assert manifest["stages"]["supply"]["execution"]["mode"] in {"direct", "isolated"}
        supply_snapshot = yaml.safe_load((result.run_dir / "resolved_configs/supply.yaml").read_text(encoding="utf-8"))
        assert supply_snapshot["fingerprint"] == manifest["stages"]["supply"]["config_fingerprint"]
        assert len(supply_snapshot["runtime_bindings"]) == 2
        assert supply_snapshot["pipeline_invocation"]["requested_to"] == "tte"
        assert manifest["stages"]["supply"]["runtime_bindings"] == supply_snapshot["runtime_bindings"]
        assert manifest["stages"]["supply"]["input_records"]["assigned_orders"]["producer_stage"] == "demand"
        assigned = load_orders(result.run_dir / "demand/orders_region_assigned.csv.gz")
        assert assigned["order_id"].tolist() == ["o-1", "000123", pd.NA]
        assert assigned["driver_id"].tolist() == ["driver-A", "driver-A", "司机甲"]
    direct_manifest = load_manifest(direct.run_dir)
    isolated_manifest = load_manifest(isolated.run_dir)
    for stage in STAGE_ORDER:
        assert set(direct_manifest["stages"][stage]["outputs"]) == set(isolated_manifest["stages"][stage]["outputs"])
        assert direct_manifest["stages"][stage]["contract"] == isolated_manifest["stages"][stage]["contract"]
        for manifest in (direct_manifest, isolated_manifest):
            for record in manifest["stages"][stage]["outputs"].values():
                assert sha256_file(record["path"]) == record["sha256"]
    direct_mapping = pd.read_csv(next((direct.run_dir / "partition/clusters").glob("*.csv"))).sort_values("seg_id")
    isolated_mapping = pd.read_csv(next((isolated.run_dir / "partition/clusters").glob("*.csv"))).sort_values("seg_id")
    pd.testing.assert_frame_equal(direct_mapping.reset_index(drop=True), isolated_mapping.reset_index(drop=True))
    with np.load(direct.run_dir / "demand/od_tensor_15min.npz") as left, np.load(
        isolated.run_dir / "demand/od_tensor_15min.npz",
    ) as right:
        assert left.files == right.files
        for name in left.files:
            np.testing.assert_array_equal(left[name], right[name])
    for name in [
        "cluster_index.csv", "cluster_od_15min.csv", "cluster_graph_road_edges.csv",
        "cluster_graph_poi_edges.csv", "cluster_graph_distance_edges.csv",
        "cluster_poi_features.csv", "cluster_poi_category_mapping.csv",
    ]:
        pd.testing.assert_frame_equal(
            pd.read_csv(direct.run_dir / "demand" / name),
            pd.read_csv(isolated.run_dir / "demand" / name),
            check_dtype=False,
        )
    for name in [
        "supply_inservice_od.csv.gz", "supply_available_floor.csv.gz", "supply_fleet_lower_bound.csv.gz",
    ]:
        pd.testing.assert_frame_equal(
            pd.read_csv(direct.run_dir / "supply" / name),
            pd.read_csv(isolated.run_dir / "supply" / name),
            check_dtype=False,
        )
    for name in ["TTE_raw.parquet", "TTE_count.parquet", "TTE_support.parquet", "TTE_hops.parquet", "TTE_imputed.parquet"]:
        pd.testing.assert_frame_equal(
            pd.read_parquet(direct.run_dir / "tte" / name),
            pd.read_parquet(isolated.run_dir / "tte" / name),
            check_dtype=False,
        )


def test_pipeline_resume_and_overwrite_invalidate_only_downstream(tmp_path: Path) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    run_pipeline(config, run_dir=run_dir, to_stage="demand", isolate_stages=False)
    partition_hash = sha256_file(next((run_dir / "partition/clusters").glob("*.csv")))
    resumed = run_pipeline(
        config, run_dir=run_dir, from_stage="supply", resume=True, isolate_stages=False,
    )
    assert resumed.all_required_stages_complete is True
    assert sha256_file(next((run_dir / "partition/clusters").glob("*.csv"))) == partition_hash
    run_pipeline(
        config, run_dir=run_dir, from_stage="demand", to_stage="demand",
        overwrite=True, isolate_stages=False,
    )
    manifest = load_manifest(run_dir)
    assert manifest["stages"]["partition"]["status"] == "complete"
    assert manifest["stages"]["demand"]["status"] == "complete"
    assert manifest["stages"]["supply"]["status"] == "not_started"
    assert manifest["stages"]["tte"]["status"] == "not_started"
    assert list(run_dir.glob("manifest.backup.*.json"))


def test_pipeline_resume_rejects_tampered_upstream(tmp_path: Path) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    run_pipeline(config, run_dir=run_dir, to_stage="demand", isolate_stages=False)
    output = next((run_dir / "partition/clusters").glob("*.csv"))
    output.write_text(output.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ResumeConflictError, match="upstream partition"):
        run_pipeline(config, run_dir=run_dir, from_stage="supply", resume=True, isolate_stages=False)


def test_public_run_cli_works_outside_repository(tmp_path: Path) -> None:
    executable = shutil.which("roadnet-partition")
    assert executable is not None
    config = write_full_fixture(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir = tmp_path / "cli-run"
    completed = subprocess.run(
        [executable, "run", "--config", str(config.resolve()), "--run-dir", str(run_dir), "--to-stage", "partition", "--no-isolate-stages"],
        cwd=outside, env=clean_environment(), capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "pipeline: complete through partition" in completed.stdout
    assert json.loads((run_dir / ".roadnet-run").read_text(encoding="utf-8"))["run_id"]


def test_direct_interrupt_is_resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    original = stages_module._run_stage
    monkeypatch.setattr(
        stages_module, "_run_stage",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(config, run_dir=run_dir, to_stage="partition", isolate_stages=False)
    manifest = load_manifest(run_dir)
    assert manifest["status"] == "interrupted"
    assert manifest["stages"]["partition"]["status"] == "interrupted"
    assert manifest["stages"]["partition"]["execution"]["exit_code"] == 130
    assert not (run_dir / "partition" / SUCCESS_MARKER).exists()
    monkeypatch.setattr(stages_module, "_run_stage", original)
    resumed = run_pipeline(config, run_dir=run_dir, to_stage="partition", resume=True, isolate_stages=False)
    assert resumed.completed_through == "partition"


def test_isolated_launch_failure_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    original_popen = runner_module.subprocess.Popen

    def fail_worker(command, *args, **kwargs):
        if isinstance(command, list) and "roadnet_partition.pipeline.worker" in command:
            raise OSError("synthetic launch failure")
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(
        runner_module.subprocess, "Popen",
        fail_worker,
    )
    with pytest.raises(OSError, match="synthetic launch failure"):
        run_pipeline(config, run_dir=run_dir, to_stage="partition", isolate_stages=True)
    record = load_manifest(run_dir)["stages"]["partition"]
    assert record["status"] == "failed"
    assert record["execution"]["mode"] == "isolated"
    assert record["execution"]["exit_code"] is None


def test_worker_rejects_tampered_request_and_snapshot(tmp_path: Path) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    run_pipeline(config, run_dir=run_dir, to_stage="partition", isolate_stages=True)
    request_path = run_dir / "requests/partition.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    marker = json.loads((run_dir / ".roadnet-run").read_text(encoding="utf-8"))
    context = runner_module.RunContext(marker["run_id"], run_dir, config.project_root, log_dir=run_dir / "logs")
    request["expected_config_fingerprint"] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        _load_config(request, context)
    request["unexpected"] = True
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        _load_request(request_path, run_dir, "partition")


def test_abnormal_running_stage_is_normalized(tmp_path: Path) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    result = run_pipeline(config, run_dir=run_dir, to_stage="partition", isolate_stages=False)
    manifest = load_manifest(run_dir)
    manifest["stages"]["demand"] = {"status": "running", "directory": "demand"}
    manifest["status"] = "running"
    atomic_write_json(run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    context = runner_module.RunContext(result.run_id, run_dir, config.project_root, log_dir=run_dir / "logs")
    runner_module._normalize_running(context, "demand", -9, "synthetic abnormal exit")
    record = load_manifest(run_dir)["stages"]["demand"]
    assert record["status"] == "interrupted"
    assert record["execution"]["exit_code"] == -9


def test_resume_rejects_missing_completed_success_marker(tmp_path: Path) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    run_pipeline(config, run_dir=run_dir, to_stage="demand", isolate_stages=False)
    (run_dir / "demand" / SUCCESS_MARKER).unlink()
    with pytest.raises(ResumeConflictError, match="upstream demand"):
        run_pipeline(config, run_dir=run_dir, from_stage="supply", resume=True, isolate_stages=False)


def test_tte_interrupt_preserves_completed_upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    run_pipeline(config, run_dir=run_dir, to_stage="supply", isolate_stages=False)
    original = stages_module._run_stage

    def interrupt_tte(stage, resolved, context):
        if stage == "tte":
            raise KeyboardInterrupt
        return original(stage, resolved, context)

    monkeypatch.setattr(stages_module, "_run_stage", interrupt_tte)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(config, run_dir=run_dir, from_stage="tte", resume=True, isolate_stages=False)
    manifest = load_manifest(run_dir)
    assert [manifest["stages"][stage]["status"] for stage in STAGE_ORDER] == [
        "complete", "complete", "complete", "interrupted",
    ]
    assert not (run_dir / "tte" / SUCCESS_MARKER).exists()


def test_direct_log_open_failure_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    original_open = Path.open

    def fail_stdout(path, *args, **kwargs):
        if path.name == "partition.stdout.log":
            raise OSError("synthetic log failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_stdout)
    with pytest.raises(OSError, match="synthetic log failure"):
        run_pipeline(config, run_dir=run_dir, to_stage="partition", isolate_stages=False)
    record = load_manifest(run_dir)["stages"]["partition"]
    assert record["status"] == "failed"
    assert record["execution"]["mode"] == "direct"
    assert record["execution"]["exit_code"] is None


def test_partition_failure_and_demand_contract_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    original_run = stages_module._run_stage
    monkeypatch.setattr(
        stages_module, "_run_stage",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("synthetic Partition failure")),
    )
    partition_run = tmp_path / "partition-failure"
    with pytest.raises(RuntimeError, match="Partition failure"):
        run_pipeline(config, run_dir=partition_run, to_stage="partition", isolate_stages=False)
    assert load_manifest(partition_run)["stages"]["partition"]["status"] == "failed"

    monkeypatch.setattr(stages_module, "_run_stage", original_run)
    demand_run = tmp_path / "demand-contract"
    run_pipeline(config, run_dir=demand_run, to_stage="partition", isolate_stages=False)
    original_contract = stages_module.validate_stage_contract

    def fail_demand_contract(stage, resolved, outputs):
        if stage == "demand":
            raise StageContractError("synthetic Demand contract failure")
        return original_contract(stage, resolved, outputs)

    monkeypatch.setattr(stages_module, "validate_stage_contract", fail_demand_contract)
    with pytest.raises(StageContractError, match="Demand contract failure"):
        run_pipeline(
            config, run_dir=demand_run, from_stage="demand", to_stage="demand",
            resume=True, isolate_stages=False,
        )
    assert load_manifest(demand_run)["stages"]["demand"]["status"] == "failed"


def test_supply_child_nonzero_exit_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    run_pipeline(config, run_dir=run_dir, to_stage="demand", isolate_stages=False)

    class FailedChild:
        stdout = io.StringIO("")
        stderr = io.StringIO("synthetic Supply child failure\n")

        def poll(self):
            return 7

        def wait(self, timeout=None):
            return 7

        def send_signal(self, _signum):
            return None

        def kill(self):
            return None

    stored_git = load_manifest(run_dir)["git"]
    monkeypatch.setattr(runner_module, "collect_git_info", lambda _root: stored_git)
    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *_args, **_kwargs: FailedChild())
    with pytest.raises(RunConflictError, match="supply worker exited with code 7"):
        run_pipeline(
            config, run_dir=run_dir, from_stage="supply", to_stage="supply",
            resume=True, isolate_stages=True,
        )
    record = load_manifest(run_dir)["stages"]["supply"]
    assert record["status"] == "failed"
    assert record["execution"]["exit_code"] == 7


@pytest.mark.parametrize("isolate", [False, True])
def test_runtime_provenance_change_invalidates_from_partition_before_resume(
    isolate: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = resolve_pipeline_config(write_full_fixture(tmp_path / "project"))
    run_dir = tmp_path / "run"
    run_pipeline(config, run_dir=run_dir, isolate_stages=isolate)
    capsys.readouterr()
    stored = load_manifest(run_dir)
    changed_runtime = deepcopy(stored["runtime"])
    numpy_record = next(record for record in changed_runtime["distributions"] if record["normalized_name"] == "numpy")
    numpy_record["version"] = f"{numpy_record['version']}-synthetic"
    changed_runtime["digest"] = manifests_module._canonical_digest(changed_runtime)
    monkeypatch.setattr(runner_module, "collect_runtime_info", lambda: changed_runtime)
    monkeypatch.setattr(runner_module, "collect_git_info", lambda _root: stored["git"])

    run_pipeline(
        config, run_dir=run_dir, from_stage="supply", to_stage="supply",
        resume=True, isolate_stages=isolate,
    )
    output = capsys.readouterr().out
    manifest = load_manifest(run_dir)
    assert "partition: reused" not in output
    assert "demand: reused" not in output
    assert manifest["pipeline"]["last_provenance_decision"]["runtime"] == ["runtime_dependency_changed"]
    assert [manifest["stages"][stage]["status"] for stage in STAGE_ORDER] == [
        "complete", "complete", "complete", "not_started",
    ]
