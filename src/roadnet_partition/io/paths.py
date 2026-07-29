from __future__ import annotations

from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when an operation would escape an explicitly owned directory."""


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
    if raw_target.is_symlink():
        raise UnsafePathError(f"symbolic-link target is not owned: {raw_target}")
    target_path = raw_target.resolve()
    if target_path == owner_path:
        if allow_owner:
            return target_path
        raise UnsafePathError("operation may not target the owner directory itself")
    if not target_path.is_relative_to(owner_path):
        raise UnsafePathError(f"target escapes owned directory: {target_path}")

    relative = target_path.relative_to(owner_path)
    current = owner_path
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise UnsafePathError(f"owned path contains a symbolic link: {current}")
    return target_path
