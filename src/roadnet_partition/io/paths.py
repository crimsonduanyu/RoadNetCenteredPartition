from __future__ import annotations

from pathlib import Path
import os
import shutil
from typing import Callable


class UnsafePathError(ValueError):
    """Raised when an operation would escape an explicitly owned directory."""


class ScopeSwapError(RuntimeError):
    """Raised when a transactional directory replacement cannot complete."""


def resolve_path(value: str | Path, *, base_dir: Path) -> Path:
    """Resolve a path without consulting the process working directory."""
    base = Path(base_dir).expanduser().resolve()
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def assert_safe_run_dir(run_dir: Path, project_root: Path) -> Path:
    """Allow external run roots while rejecting project data/release locations."""
    root = Path(project_root).expanduser().resolve()
    candidate = Path(run_dir).expanduser().resolve()
    protected = [
        root,
        root / "data",
        root / "artifacts/golden",
        root / "releases",
    ]
    for location in protected:
        location = location.resolve()
        if candidate == location or (location != root and candidate.is_relative_to(location)):
            raise UnsafePathError(f"run directory is protected: {candidate}")
    return candidate


def assert_owned_path(target: Path, owner: Path, *, allow_owner: bool = False) -> Path:
    """Validate a deletion/replacement target without following owned symlinks."""
    owner_path = Path(owner).expanduser().resolve()
    raw_target = Path(target).expanduser()
    lexical_target = raw_target if raw_target.is_absolute() else owner_path / raw_target
    lexical_target = Path(os.path.normpath(lexical_target))
    if lexical_target != owner_path and not lexical_target.is_relative_to(owner_path):
        raise UnsafePathError(f"target escapes owned directory: {lexical_target}")
    current = owner_path
    for part in lexical_target.relative_to(owner_path).parts:
        current = current / part
        if current.is_symlink():
            raise UnsafePathError(f"owned path contains a symbolic link: {current}")

    target_path = lexical_target.resolve()
    if target_path == owner_path:
        if allow_owner:
            return target_path
        raise UnsafePathError("operation may not target the owner directory itself")
    if not target_path.is_relative_to(owner_path):
        raise UnsafePathError(f"target escapes owned directory: {target_path}")

    return target_path


def transactional_scope_swap(
    target: Path,
    staging: Path,
    *,
    validate: Callable[[Path], bool | None],
    overwrite: bool = False,
    _step_hook: Callable[[str], None] | None = None,
) -> None:
    """Atomically switch one complete sibling staging directory into place."""
    raw_target = Path(target).expanduser()
    raw_staging = Path(staging).expanduser()
    if not raw_target.name or raw_target.is_symlink() or raw_staging.is_symlink():
        raise UnsafePathError("scope target and staging must be ordinary named paths")
    target_path = raw_target.resolve()
    staging_path = raw_staging.resolve()
    if target_path.parent != staging_path.parent:
        raise UnsafePathError("scope staging directory must be a sibling of the target")
    expected_prefix = f".{target_path.name}.staging-"
    if not staging_path.name.startswith(expected_prefix):
        raise UnsafePathError(f"staging directory must start with {expected_prefix!r}")
    if not staging_path.is_dir():
        raise FileNotFoundError(f"staging directory does not exist: {staging_path}")

    backup = target_path.parent / f".{target_path.name}.backup"
    other_staging = [
        path for path in target_path.parent.glob(f"{expected_prefix}*")
        if path.resolve() != staging_path
    ]
    if backup.exists() or backup.is_symlink():
        raise ScopeSwapError(f"leftover backup requires manual resolution: {backup}")
    if other_staging:
        raise ScopeSwapError(f"leftover staging directories require manual resolution: {other_staging}")

    validation_result = validate(staging_path)
    if validation_result is False:
        raise ScopeSwapError("staging validation failed")
    if _step_hook:
        _step_hook("validated")
    if target_path.exists() and not overwrite:
        raise FileExistsError(f"scope already exists; explicit overwrite required: {target_path}")

    old_moved = False
    new_moved = False
    try:
        if target_path.exists():
            if not target_path.is_dir() or target_path.is_symlink():
                raise UnsafePathError(f"scope target is not an ordinary directory: {target_path}")
            os.replace(target_path, backup)
            old_moved = True
            if _step_hook:
                _step_hook("old_moved_to_backup")

        os.replace(staging_path, target_path)
        new_moved = True
        if _step_hook:
            _step_hook("staging_moved_to_target")

        if old_moved:
            if _step_hook:
                _step_hook("before_backup_cleanup")
            shutil.rmtree(backup)
    except BaseException as error:
        rollback_errors = []
        try:
            if new_moved and target_path.exists():
                os.replace(target_path, staging_path)
        except BaseException as rollback_error:
            rollback_errors.append(rollback_error)
        try:
            if old_moved and backup.exists():
                os.replace(backup, target_path)
        except BaseException as rollback_error:
            rollback_errors.append(rollback_error)
        if rollback_errors:
            raise ScopeSwapError(f"scope swap failed and rollback was incomplete: {rollback_errors}") from error
        raise ScopeSwapError("scope swap failed; previous scope restored") from error
