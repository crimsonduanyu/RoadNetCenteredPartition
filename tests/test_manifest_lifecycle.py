from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from roadnet_partition.io.manifests import (
    MANIFEST_FILENAME,
    RUN_MARKER,
    STAGE_RESULT_FILENAME,
    SUCCESS_MARKER,
    assert_run_fingerprints,
    atomic_write_json,
    atomic_write_yaml,
    begin_stage,
    collect_git_info,
    collect_runtime_info,
    complete_stage,
    end_stage_with_status,
    evaluate_resume,
    file_record,
    initialize_run,
    input_fingerprint,
    invalidate_from_stage,
    load_manifest,
    sha256_file,
    validate_manifest,
    validate_resume_overwrite,
    verify_run_ownership,
)
from roadnet_partition.pipeline.results import RunContext, StageResult, StageStatus


def make_run(tmp_path: Path, run_id: str = "run-001") -> RunContext:
    project = tmp_path / "project"
    project.mkdir()
    config = project / "config.yaml"
    config.write_text("scope: test\n", encoding="utf-8")
    return initialize_run(
        tmp_path / "external-runs" / run_id,
        run_id=run_id,
        scope="test",
        project_root=project,
        config_source=config,
        config_values={"scope": "test"},
        config_fingerprint="config-v1",
        inputs={},
    )


def complete_fixture_stage(context: RunContext, stage: str, content: str = "result") -> tuple[RunContext, Path]:
    stage_context = context.for_stage(stage)
    begin_stage(stage_context, config_fingerprint="stage-config-v1", inputs_fingerprint="inputs-v1")
    output = stage_context.stage_dir / "result.txt"
    output.write_text(content, encoding="utf-8")
    complete_stage(
        stage_context,
        StageResult(
            stage,
            StageStatus.COMPLETE,
            {"result": output},
            {"rows": 1},
            {"status": "passed", "validated": True},
        ),
        config_fingerprint="stage-config-v1",
        inputs_fingerprint="inputs-v1",
    )
    return stage_context, output


def test_streaming_sha256_cases(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert sha256_file(empty) == hashlib.sha256(b"").hexdigest()

    first = tmp_path / "first"
    second = tmp_path / "second"
    content = b"abc" * 20
    first.write_bytes(content)
    second.write_bytes(content)
    assert sha256_file(first, block_size=7) == sha256_file(second, block_size=5)
    second.write_bytes(content + b"changed")
    assert sha256_file(first) != sha256_file(second)

    large = tmp_path / "large"
    large.write_bytes(b"0123456789" * 100)
    assert sha256_file(large, block_size=13) == hashlib.sha256(large.read_bytes()).hexdigest()
    with pytest.raises(FileNotFoundError):
        sha256_file(tmp_path / "missing")
    with pytest.raises(IsADirectoryError):
        sha256_file(tmp_path)


def test_hash_symlink_requires_explicit_follow(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("content", encoding="utf-8")
    link = tmp_path / "link"
    try:
        os.symlink(source, link)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ValueError):
        sha256_file(link)
    assert sha256_file(link, follow_symlinks=True) == sha256_file(source)


def test_atomic_json_and_yaml_are_reloaded_and_leave_no_temp_files(tmp_path: Path) -> None:
    json_path = tmp_path / "value.json"
    yaml_path = tmp_path / "value.yaml"
    value = {"z": 1, "a": Path("relative/path")}
    atomic_write_json(json_path, value)
    atomic_write_yaml(yaml_path, value)
    assert list(json.loads(json_path.read_text(encoding="utf-8"))) == ["a", "z"]
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8")) == {"a": "relative/path", "z": 1}
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".value.")]


def test_runtime_uses_dependency_allowlist_without_environment(tmp_path: Path) -> None:
    runtime = collect_runtime_info()
    assert runtime["roadnet_partition"]
    assert "numpy" in runtime["dependencies"]
    assert "environment" not in runtime


def test_git_info_records_dirty_files_and_stable_diff_hash(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    tracked.write_text("after\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("private-local-change\n", encoding="utf-8")

    first = collect_git_info(repo)
    second = collect_git_info(repo)
    assert first == second
    assert first["dirty"] is True
    assert sorted(first["changed_files"]) == ["tracked.txt", "untracked.txt"]
    assert len(first["diff_sha256"]) == 64


def test_run_creation_writes_marker_and_valid_manifest_outside_project(tmp_path: Path) -> None:
    context = make_run(tmp_path)
    marker = json.loads((context.run_dir / RUN_MARKER).read_text(encoding="utf-8"))
    manifest = verify_run_ownership(context)
    assert marker == {"run_id": "run-001", "schema_version": 1}
    assert manifest["run_id"] == "run-001"
    assert manifest["status"] == StageStatus.NOT_STARTED.value
    assert context.run_dir.parent == tmp_path / "external-runs"
    with pytest.raises(FileExistsError):
        make_run(tmp_path)


def test_manifest_schema_rejects_missing_and_arbitrary_status(tmp_path: Path) -> None:
    context = make_run(tmp_path)
    manifest = load_manifest(context.run_dir)
    validate_manifest(manifest)
    invalid = dict(manifest)
    invalid["status"] = "whatever"
    with pytest.raises(ValueError):
        validate_manifest(invalid)
    del invalid["scope"]
    with pytest.raises(ValueError):
        validate_manifest(invalid)
    with pytest.raises(TypeError):
        StageResult("partition", "complete")  # type: ignore[arg-type]


def test_stage_lifecycle_and_resume_accept_exact_complete_output(tmp_path: Path) -> None:
    context = make_run(tmp_path)
    stage_context, output = complete_fixture_stage(context, "partition")
    manifest = load_manifest(context.run_dir)
    record = manifest["stages"]["partition"]
    assert record["status"] == StageStatus.COMPLETE.value
    assert record["metrics"] == {"rows": 1}
    assert record["contract"] == {"status": "passed", "validated": True}
    stage_result = json.loads((stage_context.stage_dir / STAGE_RESULT_FILENAME).read_text(encoding="utf-8"))
    assert stage_result["status"] == StageStatus.COMPLETE.value
    assert stage_result["contract"] == {"status": "passed", "validated": True}
    assert (stage_context.stage_dir / SUCCESS_MARKER).exists()
    decision = evaluate_resume(
        stage_context,
        config_fingerprint="stage-config-v1",
        inputs_fingerprint="inputs-v1",
        required_outputs={"result": output},
    )
    assert decision.reusable is True
    assert decision.reasons == ()


@pytest.mark.parametrize(
    "change",
    ["run_status", "success_schema", "success_stage", "success_outputs", "success_hash", "stage_result"],
)
def test_resume_rejects_invalid_lifecycle_documents(tmp_path: Path, change: str) -> None:
    context = make_run(tmp_path)
    stage_context, output = complete_fixture_stage(context, "partition")
    if change == "run_status":
        manifest = load_manifest(context.run_dir)
        manifest["status"] = StageStatus.FAILED.value
        atomic_write_json(context.run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    else:
        name = STAGE_RESULT_FILENAME if change == "stage_result" else SUCCESS_MARKER
        path = stage_context.stage_dir / name
        value = json.loads(path.read_text(encoding="utf-8"))
        if change == "success_schema":
            value["schema_version"] = 999
        elif change == "success_stage":
            value["stage"] = "demand"
        elif change == "success_outputs":
            value["outputs"] = {}
        elif change == "success_hash":
            value["outputs"]["result"]["sha256"] = "0" * 64
        else:
            value["status"] = StageStatus.FAILED.value
        atomic_write_json(path, value)
    decision = evaluate_resume(
        stage_context,
        config_fingerprint="stage-config-v1",
        inputs_fingerprint="inputs-v1",
        required_outputs={"result": output},
    )
    assert decision.reusable is False
    assert decision.reasons


def test_resume_requires_exact_output_allowlist(tmp_path: Path) -> None:
    context = make_run(tmp_path)
    stage_context, output = complete_fixture_stage(context, "partition")
    decision = evaluate_resume(
        stage_context,
        config_fingerprint="stage-config-v1",
        inputs_fingerprint="inputs-v1",
        required_outputs={"renamed": output},
    )
    assert decision.reusable is False
    assert any("allowlist" in reason for reason in decision.reasons)


@pytest.mark.parametrize("change", ["config", "input", "output", "success", "manifest"])
def test_resume_rejects_every_inconsistent_state(tmp_path: Path, change: str) -> None:
    context = make_run(tmp_path)
    stage_context, output = complete_fixture_stage(context, "partition")
    config_fingerprint = "stage-config-v1"
    inputs_fingerprint = "inputs-v1"
    if change == "config":
        config_fingerprint = "changed"
    elif change == "input":
        inputs_fingerprint = "changed"
    elif change == "output":
        output.write_text("tampered", encoding="utf-8")
    elif change == "success":
        (stage_context.stage_dir / SUCCESS_MARKER).unlink()
    else:
        manifest = load_manifest(context.run_dir)
        manifest["stages"]["partition"]["status"] = StageStatus.FAILED.value
        atomic_write_json(context.run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    decision = evaluate_resume(
        stage_context,
        config_fingerprint=config_fingerprint,
        inputs_fingerprint=inputs_fingerprint,
        required_outputs={"result": output},
    )
    assert decision.reusable is False
    assert decision.reasons


def test_failed_and_interrupted_stage_states_are_recorded(tmp_path: Path) -> None:
    context = make_run(tmp_path)
    failed = context.for_stage("demand")
    begin_stage(failed, config_fingerprint="c", inputs_fingerprint="i")
    end_stage_with_status(failed, StageStatus.FAILED, "boom")
    assert load_manifest(context.run_dir)["stages"]["demand"]["error"] == "boom"
    begin_stage(failed, config_fingerprint="c", inputs_fingerprint="i")
    end_stage_with_status(failed, StageStatus.INTERRUPTED)
    assert load_manifest(context.run_dir)["stages"]["demand"]["status"] == "interrupted"


def test_manifest_completion_failure_removes_success_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import roadnet_partition.io.manifests as manifests

    context = make_run(tmp_path)
    stage_context = context.for_stage("supply")
    begin_stage(stage_context, config_fingerprint="c", inputs_fingerprint="i")
    output = stage_context.stage_dir / "result.txt"
    output.write_text("result", encoding="utf-8")

    def fail_manifest_write(*_args, **_kwargs) -> None:
        raise OSError("manifest write failed")

    monkeypatch.setattr(manifests, "_write_manifest", fail_manifest_write)
    with pytest.raises(OSError, match="manifest write failed"):
        complete_stage(
            stage_context,
            StageResult("supply", StageStatus.COMPLETE, {"result": output}),
            config_fingerprint="c",
            inputs_fingerprint="i",
        )
    assert (stage_context.stage_dir / STAGE_RESULT_FILENAME).exists()
    assert not (stage_context.stage_dir / SUCCESS_MARKER).exists()


def test_stage_result_is_written_before_success_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import roadnet_partition.io.manifests as manifests

    context = make_run(tmp_path)
    stage_context = context.for_stage("tte")
    begin_stage(stage_context, config_fingerprint="c", inputs_fingerprint="i")
    output = stage_context.stage_dir / "result.txt"
    output.write_text("result", encoding="utf-8")
    writes = []
    original = manifests.atomic_write_json

    def track_write(path, value, **kwargs) -> None:
        writes.append(Path(path).name)
        original(path, value, **kwargs)

    monkeypatch.setattr(manifests, "atomic_write_json", track_write)
    complete_stage(
        stage_context,
        StageResult("tte", StageStatus.COMPLETE, {"result": output}),
        config_fingerprint="c",
        inputs_fingerprint="i",
    )
    assert writes[:2] == [STAGE_RESULT_FILENAME, SUCCESS_MARKER]


def test_resume_overwrite_and_run_fingerprint_guards(tmp_path: Path) -> None:
    context = make_run(tmp_path)
    manifest = load_manifest(context.run_dir)
    with pytest.raises(ValueError):
        validate_resume_overwrite(resume=True, overwrite=True)
    validate_resume_overwrite(resume=True, overwrite=False)
    assert_run_fingerprints(
        manifest,
        config_fingerprint="config-v1",
        inputs_fingerprint=input_fingerprint({}),
    )
    with pytest.raises(ValueError, match="new run ID"):
        assert_run_fingerprints(manifest, config_fingerprint="changed", inputs_fingerprint=input_fingerprint({}))


def test_invalidate_from_stage_deletes_only_owned_downstream_directories(tmp_path: Path) -> None:
    context = make_run(tmp_path)
    complete_fixture_stage(context, "partition")
    complete_fixture_stage(context, "demand")
    untouched = context.run_dir / "notes.txt"
    untouched.write_text("keep", encoding="utf-8")

    backup = invalidate_from_stage(
        context,
        ordered_stages=["partition", "demand", "supply", "tte"],
        from_stage="demand",
    )

    assert backup.exists()
    assert context.for_stage("partition").stage_dir.exists()
    assert not context.for_stage("demand").stage_dir.exists()
    assert context.run_dir.exists() and untouched.exists()
    assert load_manifest(context.run_dir)["stages"]["demand"]["status"] == "not_started"


def test_invalidation_rejects_manifest_directory_mismatch_and_symlink(tmp_path: Path) -> None:
    context = make_run(tmp_path)
    stage_context, _ = complete_fixture_stage(context, "demand")
    manifest = load_manifest(context.run_dir)
    manifest["stages"]["demand"]["directory"] = "other"
    atomic_write_json(context.run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    with pytest.raises(ValueError, match="does not own"):
        invalidate_from_stage(context, ordered_stages=["demand"], from_stage="demand")

    manifest["stages"]["demand"]["directory"] = "demand"
    atomic_write_json(context.run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    real = tmp_path / "real-stage"
    stage_context.stage_dir.rename(real)
    try:
        os.symlink(real, stage_context.stage_dir)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ValueError):
        invalidate_from_stage(context, ordered_stages=["demand"], from_stage="demand")


def test_file_records_are_path_size_and_streaming_hash(tmp_path: Path) -> None:
    path = tmp_path / "input"
    path.write_text("abc", encoding="utf-8")
    assert file_record(path) == {
        "path": path.resolve().as_posix(),
        "size": 3,
        "sha256": hashlib.sha256(b"abc").hexdigest(),
    }
