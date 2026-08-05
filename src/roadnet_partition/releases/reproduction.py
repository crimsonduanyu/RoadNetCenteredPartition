from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from typing import Any, Callable, Mapping
import uuid

from roadnet_partition.io.manifests import (
    atomic_write_json,
    atomic_write_text,
    load_manifest,
    sha256_file,
    utc_now,
)
from roadnet_partition.io.paths import transactional_scope_swap
from roadnet_partition.io.safe_graph import executable_serialization_files
from roadnet_partition.pipeline.publishing import _dirty_git, build_publish_inventory
from roadnet_partition.pipeline.validation import _markdown, validate_run


class ExportError(RuntimeError):
    pass


_PROFILE = re.compile(r"^(minimal|full)$")
_PRIVATE = {"private", "restricted", "unknown"}
_RELEASE_ROOT_MARKER = ".roadnet-release-root"
_RELEASE_ROOT_MARKER_CONTENT = "roadnet-partition release root v1\n"


def _ownership_error(reason: str) -> ExportError:
    return ExportError(f"release destination is outside the allowed release ownership boundary: {reason}")


def _resolve_without_symlinks(path: Path) -> Path:
    if ".." in path.parts:
        raise _ownership_error("parent traversal is not allowed")
    absolute = path.absolute()
    for current in (absolute, *absolute.parents):
        if current.is_symlink():
            raise _ownership_error("symbolic-link path components are not allowed")
        if current != absolute and current.exists() and not current.is_dir():
            raise _ownership_error("a path component is not a directory")
    return absolute.resolve()


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _release_root(project_root: Path) -> Path:
    project = Path(project_root).expanduser().resolve()
    return project.parent / f"{project.name}-releases"


def _safe_destination(
    output: str | Path,
    run_dir: Path,
    project_root: Path,
) -> tuple[Path, Path, bool]:
    project = Path(project_root).expanduser().resolve()
    run = Path(run_dir).expanduser().resolve()
    release_root = _resolve_without_symlinks(_release_root(project))
    raw_output = Path(output).expanduser()
    destination = _resolve_without_symlinks(
        raw_output if raw_output.is_absolute() else release_root / raw_output,
    )
    protected = (project, run, (project / "data").resolve())
    if release_root == Path("/") or any(_paths_overlap(release_root, root) for root in protected):
        raise _ownership_error("a distinct release root cannot be established")
    if destination == Path("/") or any(_paths_overlap(destination, root) for root in protected):
        raise _ownership_error("the target overlaps a protected root")
    if destination.parent != release_root or not destination.name or destination.name in {".", ".."}:
        raise _ownership_error("the target must be a direct child of the release root")
    if destination.exists() and not destination.is_dir():
        raise _ownership_error("an existing target is not an ordinary directory")

    root_exists = release_root.exists()
    if root_exists:
        if not release_root.is_dir():
            raise _ownership_error("the release root is not an ordinary directory")
        marker = release_root / _RELEASE_ROOT_MARKER
        try:
            marker_valid = (
                not marker.is_symlink()
                and marker.is_file()
                and marker.read_text(encoding="utf-8") == _RELEASE_ROOT_MARKER_CONTENT
            )
        except (OSError, UnicodeError):
            marker_valid = False
        if not marker_valid:
            raise _ownership_error("the release root is not owned by this exporter")
    return destination, release_root, root_exists


def _create_release_root(release_root: Path) -> None:
    try:
        release_root.mkdir()
        (release_root / _RELEASE_ROOT_MARKER).write_text(
            _RELEASE_ROOT_MARKER_CONTENT,
            encoding="utf-8",
        )
    except OSError as error:
        raise _ownership_error("the release root could not be created safely") from error


def _classification(stage: str, key: str, scope: str) -> str:
    if stage == "partition":
        return "synthetic" if scope == "tiny" else "unknown"
    if stage == "demand" and key == "metadata":
        return "synthetic" if scope == "tiny" else "unknown"
    if stage == "demand" and key in {"orders_region_assigned", "poi_features", "poi_category_mapping", "poi_graph_edges", "poi_adjacency_raw", "poi_adjacency_normalized"}:
        return "private"
    if stage == "supply" and key in {"run_summary", "config_used"}:
        return "synthetic" if scope == "tiny" else "unknown"
    return "restricted"


def _selected_stage_files(run_dir: Path, profile: str, scope: str) -> list[dict[str, Any]]:
    published = build_publish_inventory(run_dir)
    if profile == "minimal":
        selected = [
            item for item in published
            if item["stage"] == "partition"
            or (item["stage"] == "demand" and item["logical_key"] == "metadata")
            or (item["stage"] == "supply" and item["logical_key"] in {"run_summary", "config_used"})
        ]
    else:
        selected = published
    for item in selected:
        item["classification"] = _classification(item["stage"], item["logical_key"], scope)
        if item["stage"] == "partition":
            item["release_path"] = item["formal_relative_path"]
        elif item["stage"] == "demand":
            item["release_path"] = f"metadata/demand_{Path(item['formal_relative_path']).name}"
        elif item["stage"] == "supply" and profile == "minimal":
            item["release_path"] = f"metadata/supply_{Path(item['formal_relative_path']).name}"
        else:
            item["release_path"] = f"optional/{item['formal_relative_path']}"
    return selected


def _sanitized_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest["schema_version"],
        "run_id": manifest["run_id"],
        "run_kind": manifest.get("run_kind"),
        "scope": manifest["scope"],
        "status": manifest["status"],
        "created_at": manifest["created_at"],
        "git": manifest["git"],
        "runtime": manifest["runtime"],
        "pipeline": manifest.get("pipeline"),
        "stages": {
            stage: {
                "status": record.get("status"),
                "config_fingerprint": record.get("config_fingerprint"),
                "input_fingerprint": record.get("input_fingerprint"),
                "outputs": {
                    key: {"size": value["size"], "sha256": value["sha256"]}
                    for key, value in record.get("outputs", {}).items()
                },
                "metrics": record.get("metrics"),
                "contract": record.get("contract"),
            }
            for stage, record in manifest["stages"].items()
        },
    }


def _readme(manifest: Mapping[str, Any], profile: str, selected: list[dict[str, Any]]) -> str:
    included = "\n".join(f"- `{item['release_path']}` ({item['classification']})" for item in selected)
    return f"""# RoadNet reproduction package

Source run: `{manifest['run_id']}`  
Git commit: `{manifest.get('git', {}).get('commit')}`  
Profile: `{profile}`

## Execution

Recreate the pipeline with the resolved configurations in `configs/` and the
external inputs named there. Verify this package with:

```bash
sha256sum -c checksums.sha256
```

## Included

{included}

Generated provenance, validation results, and sanitized run metadata are also
included. Raw orders, Golden payloads, driver-level records, and every file not
listed in `manifest.json` are excluded.

## Data use

This package does not invent or grant a data license. Paths and classifications
describe provenance only. **Successful generation does not imply permission for
public distribution.** Confirm all upstream data rights before sharing.
"""


def _write_payload(
    staging: Path,
    run_dir: Path,
    manifest: Mapping[str, Any],
    validation: Mapping[str, Any],
    profile: str,
    selected: list[dict[str, Any]],
    git: Mapping[str, Any],
) -> None:
    (staging / "configs/stages").mkdir(parents=True)
    shutil.copy2(run_dir / "resolved_config.yaml", staging / "configs/pipeline.yaml")
    for stage in ("partition", "demand", "supply", "tte"):
        shutil.copy2(run_dir / "resolved_configs" / f"{stage}.yaml", staging / "configs/stages" / f"{stage}.yaml")
    for item in selected:
        destination = staging / item["release_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item["run_source_path"], destination)
    atomic_write_json(staging / "metadata/run_manifest.json", _sanitized_manifest(manifest))
    atomic_write_json(staging / "reports/validation_report.json", validation)
    atomic_write_text(staging / "reports/validation_report.md", _markdown(validation))
    provenance = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "source_run_id": manifest["run_id"],
        "source_git": manifest["git"],
        "export_git": git,
        "pipeline_config_fingerprint": manifest["pipeline"]["config_fingerprint"],
        "profile": profile,
        "files": selected,
    }
    atomic_write_json(staging / "metadata/source_manifest.json", provenance)
    atomic_write_text(staging / "README.md", _readme(manifest, profile, selected))

    payload = []
    for path in sorted(item for item in staging.rglob("*") if item.is_file()):
        relative = path.relative_to(staging).as_posix()
        payload.append({
            "path": relative,
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
            "classification": next(
                (item["classification"] for item in selected if item["release_path"] == relative),
                "public" if relative == "README.md" else "synthetic" if manifest["scope"] == "tiny" else "unknown",
            ),
        })
    release_manifest = {
        "schema_version": 1,
        "source_run_id": manifest["run_id"],
        "scope": manifest["scope"],
        "profile": profile,
        "generated_at": utc_now(),
        "files": payload,
        "excluded": ["raw orders", "Golden payload", "driver-level data", "unallowlisted run artifacts"],
        "distribution_notice": "successful generation does not imply permission for public distribution",
    }
    atomic_write_json(staging / "manifest.json", release_manifest)
    checksum_paths = sorted(path for path in staging.rglob("*") if path.is_file() and path.name != "checksums.sha256")
    checksums = "".join(f"{sha256_file(path)}  {path.relative_to(staging).as_posix()}\n" for path in checksum_paths)
    atomic_write_text(staging / "checksums.sha256", checksums)


def _validate_release(path: Path) -> bool:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ExportError("release manifest schema differs")
    pickles = executable_serialization_files(path)
    if pickles:
        raise ExportError(f"reproduction bundle contains executable serialization: {pickles}")
    checksum_lines = (path / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    for line in checksum_lines:
        digest, relative = line.split("  ", 1)
        target = path / relative
        if not target.is_file() or sha256_file(target) != digest:
            raise ExportError(f"release checksum differs: {relative}")
    expected = {item["path"] for item in manifest["files"]} | {"manifest.json", "checksums.sha256"}
    actual = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}
    if actual != expected:
        raise ExportError(f"release allowlist differs: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")
    return True


def export_reproduction(
    run: str | Path,
    *,
    output: str | Path,
    profile: str = "minimal",
    overwrite: bool = False,
    allow_dirty: bool = False,
    dry_run: bool = False,
    _step_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not _PROFILE.fullmatch(profile):
        raise ExportError(f"unknown reproduction profile: {profile!r}")
    run_dir = Path(run).expanduser().resolve()
    validation = validate_run(run_dir, write_report=False)
    if validation["overall_status"] != "passed":
        raise ExportError("current run validation failed")
    manifest = load_manifest(run_dir)
    project_root = Path(manifest["config"]["resolved"]["project_root"]).resolve()
    destination, release_root, release_root_exists = _safe_destination(output, run_dir, project_root)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"release already exists; use --overwrite: {destination}")
    git = _dirty_git(manifest, project_root, allow_dirty)
    selected = _selected_stage_files(run_dir, profile, str(manifest["scope"]))
    blocked = sorted({item["classification"] for item in selected} & _PRIVATE)
    result = {
        "schema_version": 1,
        "status": "dry_run" if dry_run else "exported",
        "run_id": manifest["run_id"],
        "profile": profile,
        "output": destination.as_posix(),
        "file_count": len(selected),
        "total_size": sum(item["size"] for item in selected),
        "blocked_classifications": blocked,
        "blocking_reason": (
            None if not blocked
            else "real export rejects private, restricted, and unknown assets"
        ),
        "inventory": [
            {
                "stage": item["stage"],
                "logical_key": item["logical_key"],
                "release_path": item["release_path"],
                "classification": item["classification"],
                "size": item["size"],
            }
            for item in selected
        ],
        "git": git,
    }
    if dry_run:
        return result
    if blocked:
        raise ExportError(f"reproduction export contains blocked classifications: {blocked}")
    if not release_root_exists:
        _create_release_root(release_root)
    staging = release_root / f".{destination.name}.staging-{manifest['run_id']}-{uuid.uuid4().hex[:8]}"
    staging.mkdir()
    _write_payload(staging, run_dir, manifest, validation, profile, selected, git)

    def transaction_hook(step: str) -> None:
        if step == "staging_moved_to_target":
            _validate_release(destination)
        if _step_hook:
            _step_hook(step)

    transactional_scope_swap(
        destination, staging, allowed_parent=release_root, validate=_validate_release,
        overwrite=overwrite, _step_hook=transaction_hook,
    )
    return result


__all__ = ["ExportError", "export_reproduction"]
