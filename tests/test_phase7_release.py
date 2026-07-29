from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest

from roadnet_partition.io.manifests import MANIFEST_FILENAME, SUCCESS_MARKER, atomic_write_json, load_manifest, validate_manifest
from roadnet_partition.pipeline import publishing, validation
from roadnet_partition.pipeline.publishing import PublishError, build_publish_inventory, publish_scope
from roadnet_partition.pipeline.runner import resolve_pipeline_config, run_pipeline
from roadnet_partition.pipeline.validation import validate_run
from roadnet_partition.releases import reproduction
from roadnet_partition.releases.reproduction import ExportError, export_reproduction
from roadnet_partition.pipeline.stages import StageContractError
from test_pipeline_runner import write_full_fixture


def complete_run(tmp_path: Path, *, to_stage: str = "tte") -> tuple[Path, Path]:
    project = tmp_path / "project"
    config = resolve_pipeline_config(write_full_fixture(project))
    run_dir = tmp_path / "run"
    run_pipeline(config, run_dir=run_dir, to_stage=to_stage, isolate_stages=False)
    return project, run_dir


def semantic_report(report: dict) -> dict:
    value = deepcopy(report)
    value.pop("validated_at", None)
    return value


def test_validate_complete_run_writes_idempotent_reports_and_optional_golden(tmp_path: Path) -> None:
    _, run_dir = complete_run(tmp_path)
    first = validate_run(run_dir)
    second = validate_run(run_dir)
    assert first["overall_status"] == "passed"
    assert semantic_report(first) == semantic_report(second)
    assert (run_dir / "validation/validation_report.json").is_file()
    assert (run_dir / "validation/validation_report.md").is_file()
    manifest = load_manifest(run_dir)
    expected = manifest["stages"]["demand"]["outputs"]["cluster_index"]["sha256"]
    golden = tmp_path / "golden/manifest.json"
    golden.parent.mkdir()
    golden.write_text(json.dumps({
        "schema_version": 1,
        "checksums": [{"stage": "demand", "logical_key": "cluster_index", "sha256": expected}],
        "expected_contract": {"scope": "tiny"},
    }), encoding="utf-8")
    with_golden = validate_run(run_dir, golden=golden, write_report=False)
    assert with_golden["golden_results"]["status"] == "passed"


@pytest.mark.parametrize("damage", ["output", "success", "binding"])
def test_validate_rejects_hash_marker_and_binding_damage(tmp_path: Path, damage: str) -> None:
    _, run_dir = complete_run(tmp_path)
    manifest = load_manifest(run_dir)
    if damage == "output":
        output = Path(manifest["stages"]["supply"]["outputs"]["config_used"]["path"])
        output.write_text("{}\n", encoding="utf-8")
    elif damage == "success":
        (run_dir / "demand" / SUCCESS_MARKER).unlink()
    else:
        manifest["stages"]["tte"]["runtime_bindings"][0]["sha256"] = "0" * 64
        atomic_write_json(run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    report = validate_run(run_dir, write_report=False)
    assert report["overall_status"] == "failed"
    assert report["errors"]


def test_validate_reexecutes_contract_and_rejects_partial_or_invalid_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_dir = complete_run(tmp_path)
    monkeypatch.setattr(
        validation, "validate_stage_contract",
        lambda *_args: (_ for _ in ()).throw(StageContractError("synthetic contract failure")),
    )
    assert validate_run(run_dir, write_report=False)["overall_status"] == "failed"
    monkeypatch.undo()

    _, partial = complete_run(tmp_path / "partial", to_stage="demand")
    partial_report = validate_run(partial, write_report=False)
    assert partial_report["overall_status"] == "failed"
    assert any("completed_through" in error for error in partial_report["errors"])

    manifest_path = run_dir / MANIFEST_FILENAME
    invalid = json.loads(manifest_path.read_text(encoding="utf-8"))
    del invalid["scope"]
    manifest_path.write_text(json.dumps(invalid), encoding="utf-8")
    assert validate_run(run_dir, write_report=False)["overall_status"] == "failed"


def test_publish_dry_run_and_complete_scope_allowlist(tmp_path: Path) -> None:
    project, run_dir = complete_run(tmp_path)
    before = load_manifest(run_dir)["publish_history"]
    dry = publish_scope(run_dir, scope="tiny", dry_run=True)
    target = project / "data/processed/tiny"
    assert dry["status"] == "dry_run"
    assert not target.exists()
    assert load_manifest(run_dir)["publish_history"] == before

    result = publish_scope(run_dir, scope="tiny")
    assert result["status"] == "published"
    inventory = build_publish_inventory(run_dir)
    expected = {item["formal_relative_path"] for item in inventory} | {"source_manifest.json"}
    actual = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}
    assert actual == expected
    source = json.loads((target / "source_manifest.json").read_text(encoding="utf-8"))
    assert source["source_run_id"] == load_manifest(run_dir)["run_id"]
    assert len(source["published_files"]) == len(inventory)
    assert len(load_manifest(run_dir)["publish_history"]) == 1


def test_publish_overwrite_and_rollback_after_old_and_new_switch(tmp_path: Path) -> None:
    project, run_dir = complete_run(tmp_path)
    target = project / "data/processed/tiny"
    publish_scope(run_dir, scope="tiny")
    marker = target / "old-marker.txt"
    marker.write_text("old", encoding="utf-8")
    with pytest.raises(FileExistsError):
        publish_scope(run_dir, scope="tiny")

    def fail_after_old(step: str) -> None:
        if step == "old_moved_to_backup":
            raise RuntimeError("synthetic old rename failure")

    with pytest.raises(Exception, match="restored"):
        publish_scope(run_dir, scope="tiny", overwrite=True, _step_hook=fail_after_old)
    assert marker.read_text(encoding="utf-8") == "old"
    for staging in target.parent.glob(".tiny.staging-*"):
        shutil.rmtree(staging)

    def fail_after_new(step: str) -> None:
        if step == "staging_moved_to_target":
            raise RuntimeError("synthetic new switch failure")

    with pytest.raises(Exception, match="restored"):
        publish_scope(run_dir, scope="tiny", overwrite=True, _step_hook=fail_after_new)
    assert marker.read_text(encoding="utf-8") == "old"
    for staging in target.parent.glob(".tiny.staging-*"):
        shutil.rmtree(staging)
    publish_scope(run_dir, scope="tiny", overwrite=True)
    assert not marker.exists()


def test_publish_staging_and_post_switch_validation_failures_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_dir = complete_run(tmp_path)
    target = project / "data/processed/tiny"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old", encoding="utf-8")
    original = publishing._validate_staging
    monkeypatch.setattr(
        publishing, "_validate_staging",
        lambda *_args: (_ for _ in ()).throw(PublishError("synthetic staging validation")),
    )
    with pytest.raises(Exception):
        publish_scope(run_dir, scope="tiny", overwrite=True)
    assert (target / "old.txt").is_file()
    for staging in target.parent.glob(".tiny.staging-*"):
        shutil.rmtree(staging)
    monkeypatch.setattr(publishing, "_validate_staging", original)

    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PublishError("synthetic post-switch validation")
        return original(*args)

    monkeypatch.setattr(publishing, "_validate_staging", fail_second)
    with pytest.raises(Exception, match="restored"):
        publish_scope(run_dir, scope="tiny", overwrite=True)
    assert (target / "old.txt").is_file()


def test_publish_dirty_gate_and_run_kind_requirements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, run_dir = complete_run(tmp_path)
    dirty = {"commit": "abc", "dirty": True, "changed_files": ["local.txt"], "diff_sha256": "d" * 64}
    monkeypatch.setattr(publishing, "collect_git_info", lambda _root: dirty)
    with pytest.raises(PublishError, match="allow-dirty"):
        publish_scope(run_dir, scope="tiny", dry_run=True)
    allowed = publish_scope(run_dir, scope="tiny", allow_dirty=True, dry_run=True)
    assert allowed["git"]["current"] == dirty

    _, partial = complete_run(tmp_path / "partial", to_stage="partition")
    with pytest.raises(PublishError):
        publish_scope(partial, scope="tiny", dry_run=True)

    manifest = load_manifest(run_dir)
    manifest["run_kind"] = "stage"
    atomic_write_json(run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    with pytest.raises(PublishError):
        publish_scope(run_dir, scope="tiny", dry_run=True)


def test_minimal_export_checksum_manifest_and_no_run_mutation(tmp_path: Path) -> None:
    _, run_dir = complete_run(tmp_path)
    before = (run_dir / MANIFEST_FILENAME).read_bytes()
    output = tmp_path / "release/minimal-v1"
    result = export_reproduction(run_dir, output=output, profile="minimal")
    assert result["status"] == "exported"
    assert (run_dir / MANIFEST_FILENAME).read_bytes() == before
    assert (output / "README.md").is_file()
    assert (output / "manifest.json").is_file()
    assert (output / "checksums.sha256").is_file()
    assert not list(output.rglob("orders_region_assigned.csv.gz"))
    assert not list(output.rglob("IntermediateDataForReproduce"))
    assert "does not imply permission" in (output / "README.md").read_text(encoding="utf-8")
    for line in (output / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        assert reproduction.sha256_file(output / relative) == digest


def test_export_profiles_dry_run_privacy_overwrite_and_rollback(tmp_path: Path) -> None:
    _, run_dir = complete_run(tmp_path)
    full = export_reproduction(run_dir, output=tmp_path / "full", profile="full", dry_run=True)
    assert full["blocked_classifications"] == ["private", "restricted"]
    with pytest.raises(ExportError, match="blocked classifications"):
        export_reproduction(run_dir, output=tmp_path / "full", profile="full")

    output = tmp_path / "minimal"
    export_reproduction(run_dir, output=output)
    with pytest.raises(FileExistsError):
        export_reproduction(run_dir, output=output)
    old = output / "old.txt"
    old.write_text("old", encoding="utf-8")

    def fail_after_switch(step: str) -> None:
        if step == "staging_moved_to_target":
            raise RuntimeError("synthetic release switch failure")

    with pytest.raises(Exception, match="restored"):
        export_reproduction(run_dir, output=output, overwrite=True, _step_hook=fail_after_switch)
    assert old.read_text(encoding="utf-8") == "old"


def test_export_dirty_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, run_dir = complete_run(tmp_path)
    dirty = {"commit": "abc", "dirty": True, "changed_files": ["local.txt"], "diff_sha256": "d" * 64}
    monkeypatch.setattr(publishing, "collect_git_info", lambda _root: dirty)
    with pytest.raises(PublishError, match="allow-dirty"):
        export_reproduction(run_dir, output=tmp_path / "release")
    result = export_reproduction(run_dir, output=tmp_path / "release", allow_dirty=True)
    assert result["git"]["current"] == dirty
