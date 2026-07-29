from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import pytest
import yaml

from roadnet_partition.config import resolve_supply_config
from roadnet_partition.io.manifests import (
    MANIFEST_FILENAME,
    RUN_MARKER,
    STAGE_RESULT_FILENAME,
    SUCCESS_MARKER,
    invalidate_from_stage,
    load_manifest,
)
from roadnet_partition.io.paths import UnsafePathError
from roadnet_partition.pipeline import stages
from roadnet_partition.pipeline.results import RunContext, StageResult
from roadnet_partition.pipeline.stages import ResumeConflictError, StageContractError, execute_stage


def write_supply_fixture(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    inputs = project / "inputs"
    dataset_dir = project / "configs" / "datasets"
    stage_dir = project / "configs" / "pipelines"
    inputs.mkdir(parents=True)
    dataset_dir.mkdir(parents=True)
    stage_dir.mkdir(parents=True)
    orders_path = inputs / "orders.csv.gz"
    pd.DataFrame([
        (1, 101, "2020-01-01 08:00:00", "2020-01-01 08:10:00", 1, 2, "exclusive"),
        (2, 101, "2020-01-01 08:20:00", "2020-01-01 08:30:00", 2, 1, "exclusive"),
        (3, 202, "2020-01-01 08:05:00", "2020-01-01 08:25:00", 1, 2, "carpool"),
    ], columns=[
        "order_id", "driver_id", "departure_time", "finish_time",
        "origin_cluster_id", "destination_cluster_id", "service_type",
    ]).to_csv(orders_path, index=False, compression="gzip")
    dataset = {
        "schema_version": 1,
        "project_root": "../..",
        "scope": "tiny",
        "crs": {"projected": "EPSG:32631", "geographic": "EPSG:4326"},
        "study_area": {},
    }
    (dataset_dir / "tiny.yaml").write_text(yaml.safe_dump(dataset), encoding="utf-8")
    stage = {
        "schema_version": 1,
        "dataset_config": "../datasets/tiny.yaml",
        "scope": "tiny",
        "stage3_supply": {
            "orders_path": "../../inputs/orders.csv.gz",
            "output_dir": "../../standalone/supply",
            "max_gap_minutes": 60,
            "tau_idle_minutes": 30,
            "carpool_merge_gap_s": 0,
            "slot_duration_min": 10,
            "n_blocks": 2,
        },
    }
    config_path = stage_dir / "supply.yaml"
    config_path.write_text(yaml.safe_dump(stage, sort_keys=False), encoding="utf-8")
    return config_path, orders_path


def test_real_supply_executor_lifecycle_resume_and_overwrite(tmp_path: Path) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    run_dir = tmp_path / "external-run"

    result = execute_stage(stage="supply", config=config, run_dir=run_dir, run_id="tiny-supply")

    assert result.contract["status"] == "passed"
    assert (run_dir / RUN_MARKER).is_file()
    assert (run_dir / "resolved_config.json").is_file()
    assert (run_dir / "supply" / STAGE_RESULT_FILENAME).is_file()
    assert (run_dir / "supply" / SUCCESS_MARKER).is_file()
    manifest = load_manifest(run_dir)
    assert set(manifest["stages"]) == {"supply"}
    record = manifest["stages"]["supply"]
    assert record["status"] == "complete"
    assert set(record["outputs"]) == set(result.outputs)
    assert manifest["inputs"]["files"]["assigned_orders"]["sha256"]
    assert manifest["inputs"]["files"]["cluster_universe"]["count"] == 2
    for name, path in result.outputs.items():
        assert record["outputs"][name]["sha256"]
        assert Path(record["outputs"][name]["path"]) == path.resolve()

    resumed = execute_stage(stage="supply", config=config, run_dir=run_dir, resume=True)
    assert resumed.metrics["resume_reused"] is True

    note = run_dir / "notes.txt"
    note.write_text("keep", encoding="utf-8")
    execute_stage(stage="supply", config=config, run_dir=run_dir, overwrite=True)
    assert note.read_text(encoding="utf-8") == "keep"
    assert list(run_dir.glob("manifest.backup.*.json"))
    assert load_manifest(run_dir)["stages"]["supply"]["status"] == "complete"


@pytest.mark.parametrize("mode", ["resume", "overwrite"])
def test_invalidated_stage_can_be_retried(tmp_path: Path, mode: str) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    run_dir = tmp_path / f"run-{mode}"
    execute_stage(stage="supply", config=config, run_dir=run_dir, run_id=f"tiny-{mode}")
    context = RunContext(f"tiny-{mode}", run_dir, config.project_root, log_dir=run_dir / "logs")
    invalidate_from_stage(context, ordered_stages=["supply"], from_stage="supply")

    record = load_manifest(run_dir)["stages"]["supply"]
    assert record == {"status": "not_started", "directory": "supply"}
    execute_stage(stage="supply", config=config, run_dir=run_dir, **{mode: True})
    assert load_manifest(run_dir)["stages"]["supply"]["status"] == "complete"


def test_resume_rejects_unregistered_nonempty_stage_directory(tmp_path: Path) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    run_dir = tmp_path / "run-unregistered"
    execute_stage(stage="supply", config=config, run_dir=run_dir)
    manifest = load_manifest(run_dir)
    manifest["status"] = "not_started"
    manifest["stages"] = {}
    (run_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    foreign = run_dir / "supply" / "foreign.txt"
    foreign.write_text("keep", encoding="utf-8")

    with pytest.raises(stages.RunConflictError, match="unregistered stage directory"):
        execute_stage(stage="supply", config=config, run_dir=run_dir, resume=True)
    assert foreign.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("damage", ["success", "output"])
def test_resume_rejects_missing_marker_or_modified_output(tmp_path: Path, damage: str) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    run_dir = tmp_path / "run"
    result = execute_stage(stage="supply", config=config, run_dir=run_dir)
    if damage == "success":
        (run_dir / "supply" / SUCCESS_MARKER).unlink()
    else:
        result.outputs["config_used"].write_text("{}\n", encoding="utf-8")
    with pytest.raises(ResumeConflictError):
        execute_stage(stage="supply", config=config, run_dir=run_dir, resume=True)


def test_resume_rejects_config_and_input_changes(tmp_path: Path) -> None:
    config_path, orders_path = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    run_dir = tmp_path / "run"
    execute_stage(stage="supply", config=config, run_dir=run_dir)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["stage3_supply"]["max_gap_minutes"] = 61
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ResumeConflictError, match="config fingerprint"):
        execute_stage(stage="supply", config=resolve_supply_config(config_path), run_dir=run_dir, resume=True)

    raw["stage3_supply"]["max_gap_minutes"] = 60
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    orders = pd.read_csv(orders_path)
    orders.loc[0, "finish_time"] = "2020-01-01 08:11:00"
    orders.to_csv(orders_path, index=False, compression="gzip")
    with pytest.raises(ResumeConflictError, match="input fingerprint"):
        execute_stage(stage="supply", config=resolve_supply_config(config_path), run_dir=run_dir, resume=True)


@pytest.mark.parametrize(
    ("failure", "status", "exception"),
    [
        ("stage", "failed", RuntimeError),
        ("contract", "failed", StageContractError),
        ("interrupt", "interrupted", KeyboardInterrupt),
    ],
)
def test_executor_records_failure_and_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    status: str,
    exception: type[BaseException],
) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    run_dir = tmp_path / f"run-{failure}"
    if failure == "stage":
        monkeypatch.setattr(stages, "_run_stage", lambda *_args: (_ for _ in ()).throw(RuntimeError("synthetic stage failure")))
    elif failure == "contract":
        original = stages._run_stage

        def fake_passed_contract(stage, resolved, context):
            result = original(stage, resolved, context)
            return StageResult(result.stage, result.status, result.outputs, result.metrics, {"status": "passed"})

        monkeypatch.setattr(stages, "_run_stage", fake_passed_contract)
        monkeypatch.setattr(
            stages,
            "validate_stage_contract",
            lambda *_args: (_ for _ in ()).throw(StageContractError("synthetic contract failure")),
        )
    else:
        monkeypatch.setattr(stages, "_run_stage", lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(exception):
        execute_stage(stage="supply", config=config, run_dir=run_dir)

    manifest = load_manifest(run_dir)
    assert manifest["status"] == status
    assert manifest["stages"]["supply"]["status"] == status
    assert not (run_dir / "supply" / SUCCESS_MARKER).exists()


def test_interrupt_semantics_survive_status_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    monkeypatch.setattr(
        stages,
        "_run_stage",
        lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        stages,
        "end_stage_with_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic status failure")),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        execute_stage(stage="supply", config=config, run_dir=tmp_path / "run-interrupt-write-failure")
    assert any("could not record interrupted stage status" in note for note in caught.value.__notes__)


@pytest.mark.parametrize(
    ("exception", "status"),
    [(OSError, "failed"), (KeyboardInterrupt, "interrupted")],
)
def test_begin_stage_failure_does_not_leave_running_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception: type[BaseException],
    status: str,
) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    original = stages.begin_stage

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise exception("synthetic begin failure")

    monkeypatch.setattr(stages, "begin_stage", fail_after_write)
    run_dir = tmp_path / f"run-begin-{status}"
    with pytest.raises(exception, match="synthetic begin failure"):
        execute_stage(stage="supply", config=config, run_dir=run_dir)
    assert load_manifest(run_dir)["stages"]["supply"]["status"] == status
    assert not (run_dir / "supply" / SUCCESS_MARKER).exists()


def test_manifest_completion_failure_leaves_failed_not_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import roadnet_partition.io.manifests as manifests

    config_path, _ = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    run_dir = tmp_path / "run-manifest-failure"
    original = manifests._write_manifest

    def fail_complete(context, manifest):
        if manifest.get("status") == "complete":
            raise OSError("synthetic manifest failure")
        return original(context, manifest)

    monkeypatch.setattr(manifests, "_write_manifest", fail_complete)
    with pytest.raises(OSError, match="synthetic manifest failure"):
        execute_stage(stage="supply", config=config, run_dir=run_dir)
    assert not (run_dir / "supply" / SUCCESS_MARKER).exists()
    assert (run_dir / "supply" / STAGE_RESULT_FILENAME).exists()
    assert load_manifest(run_dir)["stages"]["supply"]["status"] == "failed"


def test_executor_rejects_swapped_output_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    original = stages._run_stage

    def swapped(stage, resolved, context):
        result = original(stage, resolved, context)
        outputs = dict(result.outputs)
        outputs["inservice_od"], outputs["available_floor"] = (
            outputs["available_floor"], outputs["inservice_od"],
        )
        return StageResult(result.stage, result.status, outputs, result.metrics, result.contract)

    monkeypatch.setattr(stages, "_run_stage", swapped)
    with pytest.raises(StageContractError, match="changed"):
        execute_stage(stage="supply", config=config, run_dir=tmp_path / "run-swapped")


def test_existing_directory_requires_explicit_mode(tmp_path: Path) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    config = resolve_supply_config(config_path)
    run_dir = tmp_path / "existing"
    run_dir.mkdir()
    with pytest.raises(stages.RunConflictError):
        execute_stage(stage="supply", config=config, run_dir=run_dir)
    with pytest.raises(ResumeConflictError):
        execute_stage(stage="supply", config=config, run_dir=run_dir, resume=True)


def test_declared_project_root_cannot_unprotect_source_repository_data(tmp_path: Path) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    resolved = resolve_supply_config(config_path)
    forged = type(resolved)(
        resolved.source_path,
        resolved.values,
        resolved.fingerprint,
        stage=resolved.stage,
        scope=resolved.scope,
        project_root=tmp_path / "forged-project",
        dataset_path=resolved.dataset_path,
    )
    source_project = Path(stages.__file__).resolve().parents[3]

    with pytest.raises(UnsafePathError):
        execute_stage(
            stage="supply",
            config=forged,
            run_dir=source_project / "data/processed/phase6a-forged-run",
        )


def test_run_manifest_contains_no_environment_or_private_rows(tmp_path: Path) -> None:
    config_path, _ = write_supply_fixture(tmp_path)
    run_dir = tmp_path / "run"
    execute_stage(stage="supply", config=resolve_supply_config(config_path), run_dir=run_dir)
    text = (run_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")
    assert "environment" not in json.loads(text)["runtime"]
    assert "driver_id" not in text
    assert "departure_time" not in text
    assert not any(key in text for key in os.environ if len(key) > 12 and key.startswith("OPENAI"))
