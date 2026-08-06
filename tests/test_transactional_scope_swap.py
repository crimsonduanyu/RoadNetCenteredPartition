from __future__ import annotations

import os
from pathlib import Path

import pytest

from roadnet_partition.io.paths import ScopeSwapError, UnsafePathError, transactional_scope_swap


def make_staging(tmp_path: Path, scope: str = "fifth_ring", value: str = "new") -> Path:
    staging = tmp_path / f".{scope}.staging-test"
    staging.mkdir()
    (staging / "content.txt").write_text(value, encoding="utf-8")
    return staging


def valid_scope(path: Path) -> bool:
    return (path / "content.txt").is_file()


def test_first_scope_publish_moves_complete_staging_directory(tmp_path: Path) -> None:
    target = tmp_path / "fifth_ring"
    staging = make_staging(tmp_path)
    transactional_scope_swap(target, staging, allowed_parent=tmp_path, validate=valid_scope)
    assert (target / "content.txt").read_text(encoding="utf-8") == "new"
    assert not staging.exists()


def test_existing_scope_requires_explicit_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "fifth_ring"
    target.mkdir()
    (target / "content.txt").write_text("old", encoding="utf-8")
    staging = make_staging(tmp_path)
    with pytest.raises(FileExistsError):
        transactional_scope_swap(target, staging, allowed_parent=tmp_path, validate=valid_scope)
    assert (target / "content.txt").read_text(encoding="utf-8") == "old"
    assert staging.exists()


def test_explicit_overwrite_replaces_whole_scope_and_removes_backup(tmp_path: Path) -> None:
    target = tmp_path / "fifth_ring"
    target.mkdir()
    (target / "old-only.txt").write_text("old", encoding="utf-8")
    staging = make_staging(tmp_path)
    transactional_scope_swap(target, staging, allowed_parent=tmp_path, validate=valid_scope, overwrite=True)
    assert (target / "content.txt").read_text(encoding="utf-8") == "new"
    assert not (target / "old-only.txt").exists()
    assert not (tmp_path / ".fifth_ring.backup").exists()


def test_validation_failure_does_not_touch_existing_scope(tmp_path: Path) -> None:
    target = tmp_path / "fifth_ring"
    target.mkdir()
    (target / "content.txt").write_text("old", encoding="utf-8")
    staging = make_staging(tmp_path)
    with pytest.raises(ScopeSwapError, match="validation"):
        transactional_scope_swap(target, staging, allowed_parent=tmp_path, validate=lambda _: False, overwrite=True)
    assert (target / "content.txt").read_text(encoding="utf-8") == "old"
    assert staging.exists()


def test_failure_after_old_rename_rolls_back(tmp_path: Path) -> None:
    target = tmp_path / "fifth_ring"
    target.mkdir()
    (target / "content.txt").write_text("old", encoding="utf-8")
    staging = make_staging(tmp_path)

    def fail_after_old_rename(step: str) -> None:
        if step == "old_moved_to_backup":
            raise RuntimeError("injected")

    with pytest.raises(ScopeSwapError, match="restored"):
        transactional_scope_swap(
            target, staging, allowed_parent=tmp_path, validate=valid_scope,
            overwrite=True, _step_hook=fail_after_old_rename,
        )
    assert (target / "content.txt").read_text(encoding="utf-8") == "old"
    assert staging.exists()
    assert not (tmp_path / ".fifth_ring.backup").exists()


def test_staging_switch_failure_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "fifth_ring"
    target.mkdir()
    (target / "content.txt").write_text("old", encoding="utf-8")
    staging = make_staging(tmp_path)
    real_replace = os.replace
    calls = 0

    def fail_second_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected staging switch failure")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_second_replace)
    with pytest.raises(ScopeSwapError, match="restored"):
        transactional_scope_swap(target, staging, allowed_parent=tmp_path, validate=valid_scope, overwrite=True)
    assert (target / "content.txt").read_text(encoding="utf-8") == "old"
    assert staging.exists()
    assert not (tmp_path / ".fifth_ring.backup").exists()


def test_failure_after_new_switch_restores_old_and_staging(tmp_path: Path) -> None:
    target = tmp_path / "fifth_ring"
    target.mkdir()
    (target / "content.txt").write_text("old", encoding="utf-8")
    staging = make_staging(tmp_path)

    def fail_after_switch(step: str) -> None:
        if step == "staging_moved_to_target":
            raise RuntimeError("injected")

    with pytest.raises(ScopeSwapError, match="restored"):
        transactional_scope_swap(target, staging, allowed_parent=tmp_path, validate=valid_scope, overwrite=True, _step_hook=fail_after_switch)
    assert (target / "content.txt").read_text(encoding="utf-8") == "old"
    assert (staging / "content.txt").read_text(encoding="utf-8") == "new"


def test_failure_before_backup_cleanup_restores_old_and_staging(tmp_path: Path) -> None:
    target = tmp_path / "fifth_ring"
    target.mkdir()
    (target / "content.txt").write_text("old", encoding="utf-8")
    staging = make_staging(tmp_path)

    def fail_before_cleanup(step: str) -> None:
        if step == "before_backup_cleanup":
            raise RuntimeError("injected")

    with pytest.raises(ScopeSwapError, match="restored"):
        transactional_scope_swap(target, staging, allowed_parent=tmp_path, validate=valid_scope, overwrite=True, _step_hook=fail_before_cleanup)
    assert (target / "content.txt").read_text(encoding="utf-8") == "old"
    assert (staging / "content.txt").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / ".fifth_ring.backup").exists()


@pytest.mark.parametrize("leftover", ["backup", "staging"])
def test_leftover_transaction_directories_are_rejected(tmp_path: Path, leftover: str) -> None:
    target = tmp_path / "fifth_ring"
    staging = make_staging(tmp_path)
    if leftover == "backup":
        (tmp_path / ".fifth_ring.backup").mkdir()
    else:
        (tmp_path / ".fifth_ring.staging-old").mkdir()
    with pytest.raises(ScopeSwapError, match="leftover"):
        transactional_scope_swap(target, staging, allowed_parent=tmp_path, validate=valid_scope)


def test_scope_swap_rejects_non_sibling_and_unmarked_staging(tmp_path: Path) -> None:
    target = tmp_path / "fifth_ring"
    other = tmp_path / "other"
    other.mkdir()
    non_sibling = make_staging(other)
    with pytest.raises(UnsafePathError, match="allowed parent"):
        transactional_scope_swap(target, non_sibling, allowed_parent=tmp_path, validate=valid_scope)

    unmarked = tmp_path / "staging"
    unmarked.mkdir()
    with pytest.raises(UnsafePathError, match="must start"):
        transactional_scope_swap(target, unmarked, allowed_parent=tmp_path, validate=lambda _: True)


def test_scope_swap_rejects_target_outside_exact_allowed_parent(tmp_path: Path) -> None:
    allowed = tmp_path / "owned"
    allowed.mkdir()
    target = tmp_path / "victim"
    staging = tmp_path / ".victim.staging-test"
    staging.mkdir()
    with pytest.raises(UnsafePathError, match="allowed parent"):
        transactional_scope_swap(
            target,
            staging,
            allowed_parent=allowed,
            validate=lambda _: True,
            overwrite=True,
        )


def test_scope_swap_rejects_filesystem_root_as_allowed_parent(tmp_path: Path) -> None:
    target = tmp_path / "target"
    staging = tmp_path / ".target.staging-test"
    staging.mkdir()
    with pytest.raises(UnsafePathError, match="narrow"):
        transactional_scope_swap(
            target,
            staging,
            allowed_parent=Path("/"),
            validate=lambda _: True,
        )


def test_scope_swap_rejects_symlinked_allowed_parent(tmp_path: Path) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    allowed_link = tmp_path / "allowed-link"
    allowed_link.symlink_to(owned, target_is_directory=True)
    target = owned / "target"
    staging = owned / ".target.staging-test"
    staging.mkdir()
    with pytest.raises(UnsafePathError, match="symbolic link"):
        transactional_scope_swap(
            target,
            staging,
            allowed_parent=allowed_link,
            validate=lambda _: True,
        )
