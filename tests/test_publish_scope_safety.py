from __future__ import annotations

from pathlib import Path

import pytest

from roadnet_partition.io.manifests import MANIFEST_FILENAME, atomic_write_json, load_manifest, validate_manifest
from roadnet_partition.pipeline import publishing
from roadnet_partition.pipeline.publishing import PublishError, publish_scope
from roadnet_partition.pipeline.validation import validate_run
from test_phase7_release import complete_run
from test_stage_config_resolvers import INVALID_SCOPES


def _forbidden(name: str):
    def fail(*_args, **_kwargs):
        pytest.fail(f"{name} must not be called before scope and containment validation")

    return fail


def _block_publish_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_disk_free", "build_publish_inventory", "_copy_inventory", "transactional_scope_swap"):
        monkeypatch.setattr(publishing, name, _forbidden(name))
    monkeypatch.setattr(publishing.uuid, "uuid4", _forbidden("staging UUID construction"))


def test_invalid_runtime_publish_scopes_fail_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run_dir = complete_run(tmp_path)
    marker = project / "marker.bin"
    unrelated = tmp_path / "unrelated.bin"
    marker.write_bytes(b"project")
    unrelated.write_bytes(b"unrelated")
    _block_publish_side_effects(monkeypatch)

    for value in INVALID_SCOPES:
        with pytest.raises(PublishError, match="invalid publish scope"):
            publish_scope(run_dir, scope=value, overwrite=True, dry_run=True)

    assert marker.read_bytes() == b"project"
    assert unrelated.read_bytes() == b"unrelated"
    assert not list(tmp_path.rglob("*.staging-*"))
    assert not list(tmp_path.rglob("*.backup"))


def test_invalid_manifest_scope_and_scope_mismatch_fail_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_dir = complete_run(tmp_path)
    manifest_path = run_dir / MANIFEST_FILENAME
    manifest = load_manifest(run_dir)
    manifest["scope"] = "../victim"
    atomic_write_json(manifest_path, manifest, validator=validate_manifest)
    _block_publish_side_effects(monkeypatch)
    with pytest.raises(PublishError, match="invalid run manifest scope"):
        publish_scope(run_dir, scope="tiny", dry_run=True)

    manifest["scope"] = "other"
    atomic_write_json(manifest_path, manifest, validator=validate_manifest)
    with pytest.raises(PublishError, match="differs from run manifest scope"):
        publish_scope(run_dir, scope="tiny", dry_run=True)


def test_valid_publish_paths_allow_nonexistent_and_existing_target(tmp_path: Path) -> None:
    processed = tmp_path / "project/data/processed"
    target = processed / "北京五环"
    staging = processed / ".北京五环.staging-run-1234"
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert publishing._validate_publish_containment(processed, target, staging, run_dir) == (
        processed.resolve(),
        target.resolve(),
        staging.resolve(),
    )
    processed.mkdir(parents=True)
    target.mkdir()
    assert publishing._validate_publish_containment(processed, target, staging, run_dir)[1] == target


@pytest.mark.parametrize(
    ("target_kind", "staging_kind", "message"),
    [
        ("root", "valid", "target is not a direct child"),
        ("ancestor", "valid", "target escapes"),
        ("outside", "valid", "target escapes"),
        ("deep", "valid", "target is not a direct child"),
        ("valid", "outside", "staging escapes"),
        ("valid", "deep", "staging is not a direct child"),
        ("valid", "equal", "must be distinct"),
    ],
)
def test_publish_containment_rejects_non_direct_or_overlapping_paths(
    tmp_path: Path,
    target_kind: str,
    staging_kind: str,
    message: str,
) -> None:
    processed = tmp_path / "project/data/processed"
    valid_target = processed / "tiny"
    valid_staging = processed / ".tiny.staging-run-1234"
    targets = {
        "root": processed,
        "ancestor": processed.parent,
        "outside": tmp_path / "outside",
        "deep": processed / "nested/tiny",
        "valid": valid_target,
    }
    stagings = {
        "valid": valid_staging,
        "outside": tmp_path / ".tiny.staging-run-1234",
        "deep": processed / "nested/.tiny.staging-run-1234",
        "equal": targets[target_kind],
    }
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(PublishError, match=message):
        publishing._validate_publish_containment(
            processed,
            targets[target_kind],
            stagings[staging_kind],
            run_dir,
        )
    assert not processed.exists()


@pytest.mark.parametrize("kind", ["target", "staging", "processed"])
def test_publish_containment_rejects_symlink_components(tmp_path: Path, kind: str) -> None:
    project_data = tmp_path / "project/data"
    project_data.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    processed = project_data / "processed"
    if kind == "processed":
        processed.symlink_to(outside, target_is_directory=True)
        target = processed / "tiny"
        staging = processed / ".tiny.staging-run-1234"
    else:
        processed.mkdir()
        target = processed / "tiny"
        staging = processed / ".tiny.staging-run-1234"
        (target if kind == "target" else staging).symlink_to(outside, target_is_directory=True)

    with pytest.raises(PublishError, match="symbolic link"):
        publishing._validate_publish_containment(processed, target, staging, run_dir)
    assert not (outside / "tiny").exists()


def test_publish_rejects_staging_escape_from_tampered_run_id_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, run_dir = complete_run(tmp_path)
    manifest = load_manifest(run_dir)
    manifest["run_id"] = "../../victim"
    atomic_write_json(run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    for name in ("_disk_free", "build_publish_inventory", "_copy_inventory", "transactional_scope_swap"):
        monkeypatch.setattr(publishing, name, _forbidden(name))

    with pytest.raises(PublishError, match="publish staging"):
        publish_scope(run_dir, scope="tiny", overwrite=True)
    assert not (tmp_path / "victim").exists()


@pytest.mark.parametrize("kind", ["processed", "target", "staging"])
def test_publish_symlink_containment_fails_before_probe_inventory_or_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    project, run_dir = complete_run(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    processed = project / "data/processed"
    processed.parent.mkdir(parents=True, exist_ok=True)
    if kind == "processed":
        processed.symlink_to(outside, target_is_directory=True)
    else:
        processed.mkdir()
        manifest = load_manifest(run_dir)
        if kind == "target":
            (processed / "tiny").symlink_to(outside, target_is_directory=True)
        else:
            token = "12345678"
            monkeypatch.setattr(publishing.uuid, "uuid4", lambda: type("Token", (), {"hex": token})())
            staging = processed / f".tiny.staging-{manifest['run_id']}-{token}"
            staging.symlink_to(outside, target_is_directory=True)
    for name in ("_disk_free", "build_publish_inventory", "_copy_inventory", "transactional_scope_swap"):
        monkeypatch.setattr(publishing, name, _forbidden(name))

    with pytest.raises(PublishError, match="symbolic link"):
        publish_scope(run_dir, scope="tiny", overwrite=True)
    assert not list(outside.iterdir())


def test_crafted_scope_cannot_write_outside_processed_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, run_dir = complete_run(tmp_path)
    validation = validate_run(run_dir, write_report=False)
    manifest = load_manifest(run_dir)
    manifest["scope"] = "../../victim"
    atomic_write_json(run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    victim = project / "victim"
    victim.mkdir()
    marker = victim / "marker.bin"
    marker.write_bytes(b"must survive")
    unrelated = tmp_path / "unrelated.bin"
    unrelated.write_bytes(b"unrelated")
    monkeypatch.setattr(publishing, "validate_run", lambda *_args, **_kwargs: validation)

    with pytest.raises(PublishError, match="invalid publish scope"):
        publish_scope(run_dir, scope="../../victim", overwrite=True, allow_dirty=True)

    assert marker.read_bytes() == b"must survive"
    assert unrelated.read_bytes() == b"unrelated"
    assert not list(project.rglob("*.staging-*"))
    assert not list(project.rglob("*.backup"))
