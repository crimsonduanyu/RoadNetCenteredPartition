from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping

import yaml

from roadnet_partition import __version__
from roadnet_partition.config import config_fingerprint as calculate_config_fingerprint, stable_value
from roadnet_partition.io.paths import assert_owned_path, assert_safe_run_dir
from roadnet_partition.pipeline.results import ResumeDecision, RunContext, StageResult, StageStatus


SCHEMA_VERSION = 1
RUN_MANIFEST_SCHEMA_VERSION = 2
RUN_MARKER = ".roadnet-run"
MANIFEST_FILENAME = "manifest.json"
SUCCESS_MARKER = "_SUCCESS"
STAGE_RESULT_FILENAME = "stage_result.json"
RESULT_AFFECTING_DISTRIBUTIONS = (
    ("duckdb", ("duckdb",), "environment.yml; columnar staging and I/O"),
    ("Fiona", ("fiona",), "environment.yml; geospatial I/O backend"),
    ("geopandas", ("geopandas",), "environment.yml and production imports"),
    ("leidenalg", ("leidenalg",), "environment.yml; Leiden clustering"),
    ("matplotlib", ("matplotlib",), "environment.yml; formal rendering entrypoints"),
    ("networkx", ("networkx",), "environment.yml and production graph algorithms"),
    ("numpy", ("numpy",), "environment.yml and production numerical kernels"),
    ("osmnx", ("osmnx",), "environment.yml; road-network construction"),
    ("pandas", ("pandas",), "environment.yml and production table processing"),
    ("pyarrow", ("pyarrow",), "environment.yml; Parquet artifacts"),
    ("pyogrio", ("pyogrio",), "environment.yml; geospatial I/O backend"),
    ("pymetis", ("pymetis",), "environment.yml; METIS partitioning"),
    ("pyproj", ("pyproj",), "environment.yml; CRS transformations"),
    ("python-igraph", ("igraph",), "environment.yml; Leiden graph backend"),
    ("python-louvain", ("community", "community.community_louvain"), "environment.yml pip dependency; Louvain clustering"),
    ("PyYAML", ("yaml",), "environment.yml and configuration loading"),
    ("Rtree", ("rtree",), "environment.yml; spatial indexing backend"),
    ("scikit-learn", ("sklearn",), "environment.yml; clustering and metrics"),
    ("scipy", ("scipy",), "environment.yml and sparse/numerical algorithms"),
    ("Shapely", ("shapely",), "environment.yml and geometry operations"),
    ("tqdm", ("tqdm",), "environment.yml; production progress iteration"),
)
NATIVE_LIBRARY_NAMES = ("blas_backend", "duckdb", "gdal", "geos", "igraph_core", "proj", "sqlite")
_SHA256 = "0123456789abcdef"


class ProvenanceError(ValueError):
    def __init__(self, reason: str, logical_path: str | None = None) -> None:
        self.reason = reason
        self.logical_path = logical_path
        super().__init__(reason if logical_path is None else f"{reason}: {logical_path}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str | Path, *, block_size: int = 8 * 1024 * 1024, follow_symlinks: bool = False) -> str:
    source = Path(path)
    if source.is_symlink() and not follow_symlinks:
        raise ValueError(f"refusing to hash symbolic link: {source}")
    if not source.exists():
        raise FileNotFoundError(source)
    if not source.is_file():
        raise IsADirectoryError(source)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(
    path: str | Path,
    value: Any,
    *,
    validator: Callable[[Any], None] | None = None,
) -> None:
    destination = Path(path)
    payload = stable_value(value)
    _atomic_write(destination, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    loaded = json.loads(destination.read_text(encoding="utf-8"))
    if validator is not None:
        validator(loaded)


def atomic_write_yaml(path: str | Path, value: Any) -> None:
    destination = Path(path)
    payload = stable_value(value)
    _atomic_write(destination, yaml.safe_dump(payload, allow_unicode=True, sort_keys=True))
    yaml.safe_load(destination.read_text(encoding="utf-8"))


def atomic_write_text(path: str | Path, value: str) -> None:
    _atomic_write(Path(path), value)


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "digest"}
    encoded = json.dumps(stable_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalized_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _unavailable(error: BaseException) -> str:
    return type(error).__name__


def _distribution_records() -> list[dict[str, Any]]:
    records = []
    for name, aliases, source in RESULT_AFFECTING_DISTRIBUTIONS:
        try:
            version = importlib.metadata.version(name)
            status_value, reason = "installed", None
        except importlib.metadata.PackageNotFoundError:
            version, status_value, reason = None, "unavailable", "distribution_not_installed"
        except Exception as error:
            version, status_value, reason = None, "unavailable", _unavailable(error)
        records.append({
            "name": name,
            "normalized_name": _normalized_distribution_name(name),
            "version": version,
            "status": status_value,
            "unavailable_reason": reason,
            "import_aliases": sorted(aliases),
            "result_affecting": True,
            "source": source,
        })
    return sorted(records, key=lambda record: record["normalized_name"])


def _native_source(module: str, attribute: str) -> dict[str, Any]:
    try:
        value: Any = importlib.import_module(module)
        for component in attribute.split("."):
            value = getattr(value, component)
        if callable(value):
            value = value()
        if isinstance(value, (tuple, list)):
            value = ".".join(str(component) for component in value)
        return {"source": f"{module}.{attribute}", "value": str(value), "unavailable_reason": None}
    except Exception as error:
        return {"source": f"{module}.{attribute}", "value": None, "unavailable_reason": _unavailable(error)}


def _native_record(*sources: dict[str, Any]) -> dict[str, Any]:
    available = {source["value"] for source in sources if source["value"] is not None}
    return {
        "sources": sorted(sources, key=lambda source: source["source"]),
        "consistent": None if not available else len(available) == 1,
    }


def _native_library_records() -> dict[str, Any]:
    records = {
        "blas_backend": _native_record({
            "source": "numpy public runtime API", "value": None,
            "unavailable_reason": "no_stable_path_free_public_api",
        }),
        "duckdb": _native_record(_native_source("duckdb", "__version__")),
        "gdal": _native_record(
            _native_source("fiona", "__gdal_version__"),
            _native_source("pyogrio", "__gdal_version__"),
        ),
        "geos": _native_record(_native_source("shapely", "geos_version_string")),
        "igraph_core": _native_record(_native_source("igraph", "__igraph_version__")),
        "proj": _native_record(_native_source("pyproj", "proj_version_str")),
        "sqlite": _native_record(_native_source("sqlite3", "sqlite_version")),
    }
    return {name: records[name] for name in sorted(records)}


def collect_runtime_info() -> dict[str, Any]:
    distributions = _distribution_records()
    runtime = {
        "schema_version": 1,
        "python": {
            "implementation": platform.python_implementation(),
            "version": " ".join(sys.version.split()),
            "executable": Path(sys.executable).name,
        },
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "architecture": f"{struct.calcsize('P') * 8}bit",
            "byte_order": sys.byteorder,
        },
        "distributions": distributions,
        "native_libraries": _native_library_records(),
        "roadnet_partition": __version__,
    }
    runtime["digest"] = _canonical_digest(runtime)
    return runtime


def _git_run(root: Path, *args: str) -> bytes:
    environment = {"LC_ALL": "C", "LANG": "C", "GIT_PAGER": "cat", "PATH": os.defpath}
    return subprocess.run(
        ["git", "-c", "color.ui=false", *args], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
    ).stdout


def _untracked_file_record(root: Path, relative: str, *, block_size: int = 8 * 1024 * 1024) -> dict[str, Any]:
    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative_path.parts or any(part in {"", ".", ".."} for part in relative_path.parts):
        raise ProvenanceError("git_untracked_path_invalid")
    candidate = root / relative_path
    try:
        before = candidate.lstat()
    except OSError as error:
        raise ProvenanceError("git_untracked_content_changed", relative) from error
    if not stat.S_ISREG(before.st_mode):
        raise ProvenanceError("git_untracked_unsupported_type", relative)
    expected_parent = root.joinpath(*relative_path.parts[:-1]).resolve(strict=False)
    if candidate.resolve(strict=False).parent != expected_parent or not expected_parent.is_relative_to(root):
        raise ProvenanceError("git_untracked_unsupported_type", relative)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ProvenanceError("git_untracked_safe_open_unavailable")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    digest = hashlib.sha256()
    try:
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size) != (
                before.st_dev, before.st_ino, before.st_mode, before.st_size,
            ):
                raise ProvenanceError("git_untracked_content_changed", relative)
            while True:
                block = os.read(descriptor, block_size)
                if not block:
                    break
                digest.update(block)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        final = candidate.lstat()
    except ProvenanceError:
        raise
    except OSError as error:
        raise ProvenanceError("git_untracked_content_changed", relative) from error
    identity = lambda value: (
        value.st_dev, value.st_ino, value.st_mode, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )
    if identity(before) != identity(after) or identity(after) != identity(final):
        raise ProvenanceError("git_untracked_content_changed", relative)
    mode = stat.S_IMODE(final.st_mode)
    return {
        "path": relative,
        "size": final.st_size,
        "sha256": digest.hexdigest(),
        "mode": f"{mode:04o}",
        "executable": bool(mode & 0o111),
        "type": "regular_file",
    }


def collect_git_info(project_root: str | Path) -> dict[str, Any]:
    root = Path(project_root).resolve()
    try:
        top = Path(os.fsdecode(_git_run(root, "rev-parse", "--show-toplevel").strip())).resolve()
        if top != root:
            raise ProvenanceError("git_repository_root_mismatch")
        commit = _git_run(root, "rev-parse", "HEAD").decode("ascii").strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        unavailable = {
            "schema_version": 2, "repository_status": "unavailable", "complete": False,
            "commit": None, "branch": None, "detached": None, "clean": None, "dirty": None,
            "status": [], "tracked_diff_sha256": None, "untracked_files": [],
            "changed_files": [], "diff_sha256": None,
        }
        unavailable["digest"] = _canonical_digest(unavailable)
        return unavailable
    except ProvenanceError:
        raise
    except UnicodeError as error:
        raise ProvenanceError("git_metadata_invalid_encoding") from error
    try:
        branch = _git_run(root, "symbolic-ref", "--quiet", "--short", "HEAD").decode("utf-8").strip()
        detached = False
    except subprocess.CalledProcessError:
        detached, branch = True, None
    except UnicodeError as error:
        raise ProvenanceError("git_metadata_invalid_encoding") from error
    status_bytes = _git_run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    tracked_diff = _git_run(root, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--")
    tracked_names_bytes = _git_run(root, "diff", "--name-only", "-z", "HEAD", "--")
    untracked_bytes = _git_run(root, "ls-files", "--others", "--exclude-standard", "-z")
    try:
        status_entries = sorted(item.decode("utf-8") for item in status_bytes.split(b"\0") if item)
        paths = sorted(item.decode("utf-8") for item in untracked_bytes.split(b"\0") if item)
        tracked_names = sorted(item.decode("utf-8") for item in tracked_names_bytes.split(b"\0") if item)
    except UnicodeError as error:
        raise ProvenanceError("git_metadata_invalid_encoding") from error
    untracked = [_untracked_file_record(root, relative) for relative in paths]
    if _git_run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") != status_bytes:
        raise ProvenanceError("git_untracked_content_changed")
    if _git_run(root, "ls-files", "--others", "--exclude-standard", "-z") != untracked_bytes:
        raise ProvenanceError("git_untracked_content_changed")
    if _git_run(root, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--") != tracked_diff:
        raise ProvenanceError("git_tracked_content_changed")
    tracked_hash = hashlib.sha256(tracked_diff).hexdigest()
    provenance = {
        "schema_version": 2,
        "repository_status": "available",
        "complete": True,
        "commit": commit,
        "branch": branch,
        "detached": detached,
        "clean": not status_entries,
        "dirty": bool(status_entries),
        "status": status_entries,
        "tracked_diff_sha256": tracked_hash,
        "untracked_files": untracked,
        "changed_files": sorted(set(tracked_names) | {record["path"] for record in untracked}),
        "diff_sha256": tracked_hash,
    }
    provenance["digest"] = _canonical_digest(provenance)
    return provenance


def file_record(path: str | Path) -> dict[str, Any]:
    source = Path(path).resolve()
    return {"path": source.as_posix(), "size": source.stat().st_size, "sha256": sha256_file(source)}


def input_fingerprint(inputs: Mapping[str, Mapping[str, Any]]) -> str:
    payload = json.dumps(stable_value(inputs), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in _SHA256 for character in value)


def validate_runtime_provenance(runtime: Any) -> None:
    if not isinstance(runtime, dict) or runtime.get("schema_version") != 1:
        raise ValueError("runtime provenance schema is unsupported")
    if set(runtime.get("native_libraries", {})) != set(NATIVE_LIBRARY_NAMES):
        raise ValueError("runtime native library records are incomplete")
    python_record = runtime.get("python")
    platform_record = runtime.get("platform")
    if not isinstance(python_record, dict) or set(python_record) != {"implementation", "version", "executable"}:
        raise ValueError("runtime Python record is invalid")
    if Path(str(python_record["executable"])).name != python_record["executable"]:
        raise ValueError("runtime executable identity must not contain a path")
    if not isinstance(platform_record, dict) or set(platform_record) != {"system", "machine", "architecture", "byte_order"}:
        raise ValueError("runtime platform record is invalid")
    records = runtime.get("distributions")
    if not isinstance(records, list):
        raise ValueError("runtime distribution records are invalid")
    names = [record.get("normalized_name") for record in records if isinstance(record, dict)]
    if len(names) != len(records) or names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("runtime distribution records must be unique and sorted")
    expected_names = sorted(_normalized_distribution_name(item[0]) for item in RESULT_AFFECTING_DISTRIBUTIONS)
    if names != expected_names:
        raise ValueError("runtime result-affecting distribution records are incomplete")
    for record in records:
        status_value = record.get("status")
        version, reason = record.get("version"), record.get("unavailable_reason")
        if status_value == "installed":
            if not isinstance(version, str) or not version or reason is not None:
                raise ValueError("installed runtime distribution record is inconsistent")
        elif status_value == "unavailable":
            if version is not None or not isinstance(reason, str) or not reason:
                raise ValueError("unavailable runtime distribution record is inconsistent")
        else:
            raise ValueError("runtime distribution status is invalid")
        if record.get("normalized_name") != _normalized_distribution_name(str(record.get("name", ""))):
            raise ValueError("runtime distribution normalized name differs")
        aliases = record.get("import_aliases")
        if not isinstance(aliases, list) or aliases != sorted(set(aliases)):
            raise ValueError("runtime distribution aliases must be unique and sorted")
    for name in NATIVE_LIBRARY_NAMES:
        native = runtime["native_libraries"][name]
        sources = native.get("sources") if isinstance(native, dict) else None
        if not isinstance(sources, list) or not sources:
            raise ValueError("runtime native library sources are invalid")
        labels = [source.get("source") for source in sources if isinstance(source, dict)]
        if len(labels) != len(sources) or labels != sorted(labels) or len(labels) != len(set(labels)):
            raise ValueError("runtime native sources must be unique and sorted")
        for source in sources:
            value, reason = source.get("value"), source.get("unavailable_reason")
            if (value is None) == (reason is None):
                raise ValueError("runtime native source availability is inconsistent")
            if isinstance(value, str) and ("/" in value or "\\" in value):
                raise ValueError("runtime native source leaks a path")
        available = {source["value"] for source in sources if source["value"] is not None}
        expected_consistency = None if not available else len(available) == 1
        if native.get("consistent") is not expected_consistency:
            raise ValueError("runtime native source consistency differs")
    if not _valid_sha256(runtime.get("digest")) or runtime["digest"] != _canonical_digest(runtime):
        raise ValueError("runtime provenance digest differs")


def validate_git_provenance(git: Any) -> None:
    if not isinstance(git, dict) or git.get("schema_version") != 2:
        raise ValueError("Git provenance schema is unsupported")
    if git.get("repository_status") == "unavailable":
        if git.get("complete") is not False:
            raise ValueError("unavailable Git provenance cannot be complete")
        if any(git.get(field) is not None for field in ("commit", "branch", "detached", "clean", "dirty", "tracked_diff_sha256")):
            raise ValueError("unavailable Git provenance contains repository state")
        if git.get("status") != [] or git.get("untracked_files") != [] or git.get("changed_files") != []:
            raise ValueError("unavailable Git provenance contains dirty records")
    elif git.get("repository_status") == "available":
        if git.get("complete") is not True or not _valid_sha256(git.get("tracked_diff_sha256")):
            raise ValueError("Git provenance is incomplete")
        records = git.get("untracked_files")
        if not isinstance(records, list):
            raise ValueError("Git untracked records are invalid")
        paths = [record.get("path") for record in records if isinstance(record, dict)]
        if len(paths) != len(records) or paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("Git untracked records must be unique and sorted")
        for record in records:
            path = record.get("path")
            path_value = Path(path) if isinstance(path, str) else Path("/")
            if path_value.is_absolute() or not path_value.parts or any(part in {"", ".", ".."} for part in path_value.parts):
                raise ValueError("Git untracked path is not repository-relative")
            if record.get("type") != "regular_file" or not isinstance(record.get("size"), int) or record["size"] < 0:
                raise ValueError("Git untracked file record is invalid")
            if not _valid_sha256(record.get("sha256")) or not re.fullmatch(r"[0-7]{4}", str(record.get("mode", ""))):
                raise ValueError("Git untracked file digest/mode is invalid")
        clean = git.get("clean")
        dirty = git.get("dirty")
        status_entries = git.get("status")
        if not isinstance(status_entries, list) or status_entries != sorted(status_entries):
            raise ValueError("Git status records must be sorted")
        if clean is not (not status_entries) or dirty is not bool(status_entries):
            raise ValueError("Git clean/dirty state is inconsistent")
        if clean and (records or git["tracked_diff_sha256"] != hashlib.sha256(b"").hexdigest()):
            raise ValueError("clean Git provenance contains dirty content")
    else:
        raise ValueError("Git repository status is invalid")
    if not _valid_sha256(git.get("digest")) or git["digest"] != _canonical_digest(git):
        raise ValueError("Git provenance digest differs")


def manifest_provenance_complete(manifest: Mapping[str, Any]) -> bool:
    if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA_VERSION:
        return False
    try:
        validate_runtime_provenance(manifest.get("runtime"))
        validate_git_provenance(manifest.get("git"))
    except ValueError:
        return False
    return (
        manifest.get("runtime_provenance_complete") is True
        and manifest.get("git_provenance_complete") is True
        and bool(manifest["git"].get("complete"))
    )


def require_complete_provenance(manifest: Mapping[str, Any]) -> None:
    if not manifest_provenance_complete(manifest):
        raise ProvenanceError("run_provenance_incomplete")


def provenance_mismatch_reasons(
    stored_runtime: Any,
    current_runtime: Mapping[str, Any],
    stored_git: Any,
    current_git: Mapping[str, Any],
) -> dict[str, Any]:
    runtime_reasons: list[str] = []
    git_reasons: list[str] = []
    if not isinstance(stored_runtime, Mapping) or stored_runtime.get("schema_version") != 1:
        runtime_reasons.append("runtime_provenance_missing_legacy_manifest")
    elif stored_runtime.get("digest") == current_runtime.get("digest"):
        runtime_reasons.append("runtime_provenance_match")
    else:
        for field, reason in (
            ("python", "runtime_python_changed"),
            ("platform", "runtime_platform_changed"),
            ("distributions", "runtime_dependency_changed"),
            ("native_libraries", "runtime_native_library_changed"),
        ):
            if stored_runtime.get(field) != current_runtime.get(field):
                runtime_reasons.append(reason)
        if (
            stored_runtime.get("roadnet_partition") != current_runtime.get("roadnet_partition")
            and "runtime_dependency_changed" not in runtime_reasons
        ):
            runtime_reasons.append("runtime_dependency_changed")
    if not isinstance(stored_git, Mapping) or stored_git.get("schema_version") != 2:
        git_reasons.append("git_provenance_missing_legacy_manifest")
    elif stored_git.get("digest") == current_git.get("digest"):
        git_reasons.append("git_provenance_match")
    else:
        if (
            stored_git.get("commit") != current_git.get("commit")
            or stored_git.get("tracked_diff_sha256") != current_git.get("tracked_diff_sha256")
        ):
            git_reasons.append("git_tracked_content_changed")
        old = {record["path"]: record for record in stored_git.get("untracked_files", [])}
        new = {record["path"]: record for record in current_git.get("untracked_files", [])}
        if old.keys() - new.keys():
            git_reasons.append("git_untracked_file_removed")
        if new.keys() - old.keys():
            git_reasons.append("git_untracked_file_added")
        if any(old[path] != new[path] for path in old.keys() & new.keys()):
            git_reasons.append("git_untracked_content_changed")
        if not git_reasons:
            git_reasons.append("git_repository_state_changed")
    return {
        "runtime": runtime_reasons,
        "git": git_reasons,
        "compatible": runtime_reasons == ["runtime_provenance_match"] and git_reasons == ["git_provenance_match"],
    }


def validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    required = {
        "schema_version", "run_id", "scope", "status", "created_at", "updated_at",
        "git", "runtime", "config", "inputs", "stages", "publish_history",
    }
    missing = required - manifest.keys()
    if missing:
        raise ValueError(f"manifest missing fields: {sorted(missing)}")
    if manifest["schema_version"] not in {SCHEMA_VERSION, RUN_MANIFEST_SCHEMA_VERSION}:
        raise ValueError(f"unsupported manifest schema: {manifest['schema_version']}")
    if manifest["schema_version"] == RUN_MANIFEST_SCHEMA_VERSION:
        validate_runtime_provenance(manifest["runtime"])
        validate_git_provenance(manifest["git"])
        experiment = manifest.get("experiment")
        if not isinstance(experiment, dict):
            raise ValueError("run manifest experiment identity is missing")
        if experiment.get("runtime_provenance_digest") != manifest["runtime"]["digest"]:
            raise ValueError("experiment runtime provenance digest differs")
        if experiment.get("git_provenance_digest") != manifest["git"]["digest"]:
            raise ValueError("experiment Git provenance digest differs")
        if manifest.get("runtime_provenance_complete") is not True:
            raise ValueError("runtime provenance completeness marker differs")
        if manifest.get("git_provenance_complete") is not bool(manifest["git"].get("complete")):
            raise ValueError("Git provenance completeness marker differs")
        if experiment.get("fingerprint") != manifest.get("config", {}).get("fingerprint"):
            raise ValueError("experiment fingerprint differs from config fingerprint")
        resolved = manifest.get("config", {}).get("resolved", {})
        if isinstance(resolved, Mapping) and "provenance" in resolved and experiment.get("fingerprint") != calculate_config_fingerprint(resolved):
            raise ValueError("experiment fingerprint cannot be recomputed from resolved config")
    if manifest["status"] not in {status.value for status in StageStatus}:
        raise ValueError(f"invalid run status: {manifest['status']}")
    if not isinstance(manifest["stages"], dict) or not isinstance(manifest["publish_history"], list):
        raise ValueError("manifest stages/publish_history have invalid types")
    for stage, record in manifest["stages"].items():
        if record.get("status") not in {status.value for status in StageStatus}:
            raise ValueError(f"invalid status for stage {stage!r}")


def new_manifest(
    *,
    run_id: str,
    scope: str,
    project_root: Path,
    config_source: Path,
    config_values: Mapping[str, Any],
    config_fingerprint: str,
    inputs: Mapping[str, Mapping[str, Any]],
    runtime: Mapping[str, Any] | None = None,
    git: Mapping[str, Any] | None = None,
    base_config_fingerprint: str | None = None,
    preparation_identity_digest: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    runtime_value = dict(runtime or collect_runtime_info())
    git_value = dict(git or collect_git_info(project_root))
    return {
        "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "scope": scope,
        "status": StageStatus.NOT_STARTED.value,
        "created_at": now,
        "updated_at": now,
        "git": git_value,
        "runtime": runtime_value,
        "runtime_provenance_complete": True,
        "git_provenance_complete": bool(git_value.get("complete")),
        "experiment": {
            "schema_version": 1,
            "base_config_fingerprint": base_config_fingerprint or config_fingerprint,
            "preparation_identity_digest": preparation_identity_digest,
            "runtime_provenance_digest": runtime_value["digest"],
            "git_provenance_digest": git_value["digest"],
            "fingerprint": config_fingerprint,
        },
        "config": {
            "source_path": config_source.resolve().as_posix(),
            "fingerprint": config_fingerprint,
            "resolved": stable_value(config_values),
        },
        "inputs": {
            "fingerprint": input_fingerprint(inputs),
            "files": stable_value(inputs),
        },
        "stages": {},
        "publish_history": [],
    }


def initialize_run(
    run_dir: str | Path,
    *,
    run_id: str,
    scope: str,
    project_root: str | Path,
    config_source: str | Path,
    config_values: Mapping[str, Any],
    config_fingerprint: str,
    inputs: Mapping[str, Mapping[str, Any]],
    runtime: Mapping[str, Any] | None = None,
    git: Mapping[str, Any] | None = None,
    base_config_fingerprint: str | None = None,
    preparation_identity_digest: str | None = None,
) -> RunContext:
    root = Path(project_root).resolve()
    destination = assert_safe_run_dir(Path(run_dir), root)
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"run directory is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "logs").mkdir()
    marker = {"schema_version": SCHEMA_VERSION, "run_id": run_id}
    atomic_write_json(destination / RUN_MARKER, marker)
    manifest = new_manifest(
        run_id=run_id,
        scope=scope,
        project_root=root,
        config_source=Path(config_source),
        config_values=config_values,
        config_fingerprint=config_fingerprint,
        inputs=inputs,
        runtime=runtime,
        git=git,
        base_config_fingerprint=base_config_fingerprint,
        preparation_identity_digest=preparation_identity_digest,
    )
    atomic_write_json(destination / MANIFEST_FILENAME, manifest, validator=validate_manifest)
    return RunContext(run_id=run_id, run_dir=destination, project_root=root, log_dir=destination / "logs")


def load_manifest(run_dir: str | Path) -> dict[str, Any]:
    manifest = json.loads((Path(run_dir) / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    validate_manifest(manifest)
    if manifest["schema_version"] == SCHEMA_VERSION:
        manifest["runtime_provenance_complete"] = False
        manifest["git_provenance_complete"] = False
    return manifest


def verify_run_ownership(context: RunContext) -> dict[str, Any]:
    marker_path = context.run_dir / RUN_MARKER
    manifest_path = context.run_dir / MANIFEST_FILENAME
    if marker_path.is_symlink() or manifest_path.is_symlink():
        raise ValueError("run ownership files may not be symbolic links")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    manifest = load_manifest(context.run_dir)
    if marker != {"schema_version": SCHEMA_VERSION, "run_id": context.run_id}:
        raise ValueError("run marker does not match context")
    if manifest["run_id"] != context.run_id:
        raise ValueError("manifest run ID does not match context")
    return manifest


def _write_manifest(context: RunContext, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = utc_now()
    atomic_write_json(context.run_dir / MANIFEST_FILENAME, manifest, validator=validate_manifest)


def begin_stage(
    context: RunContext,
    *,
    config_fingerprint: str,
    inputs_fingerprint: str,
    inputs: Mapping[str, Mapping[str, Any]] | None = None,
    runtime_bindings: list[Mapping[str, Any]] | None = None,
) -> RunContext:
    if context.stage_name is None or context.stage_dir is None:
        raise ValueError("stage context required")
    manifest = verify_run_ownership(context)
    current = manifest["stages"].get(context.stage_name, {}).get("status", StageStatus.NOT_STARTED.value)
    if current not in {StageStatus.NOT_STARTED.value, StageStatus.FAILED.value, StageStatus.INTERRUPTED.value}:
        raise ValueError(f"stage cannot start from status {current!r}")
    context.stage_dir.mkdir(parents=True, exist_ok=True)
    manifest["stages"][context.stage_name] = {
        "status": StageStatus.RUNNING.value,
        "directory": context.stage_dir.relative_to(context.run_dir).as_posix(),
        "config_fingerprint": config_fingerprint,
        "input_fingerprint": inputs_fingerprint,
        "input_records": stable_value(inputs or {}),
        "runtime_bindings": stable_value(runtime_bindings or []),
        "started_at": utc_now(),
        "outputs": {},
        "metrics": {},
        "error": None,
    }
    manifest["status"] = StageStatus.RUNNING.value
    _write_manifest(context, manifest)
    return context


def complete_stage(
    context: RunContext,
    result: StageResult,
    *,
    config_fingerprint: str,
    inputs_fingerprint: str,
) -> None:
    if result.status is not StageStatus.COMPLETE or result.stage != context.stage_name:
        raise ValueError("complete_stage requires a matching COMPLETE result")
    manifest = verify_run_ownership(context)
    record = manifest["stages"].get(result.stage)
    if record is None or record["status"] != StageStatus.RUNNING.value:
        raise ValueError("stage is not running")
    outputs = {}
    for name, path in result.outputs.items():
        owned = assert_owned_path(path, context.stage_dir)
        outputs[name] = file_record(owned)
    stage_result = {
        "schema_version": SCHEMA_VERSION,
        "stage": result.stage,
        "status": result.status.value,
        "config_fingerprint": config_fingerprint,
        "input_fingerprint": inputs_fingerprint,
        "outputs": outputs,
        "metrics": stable_value(result.metrics),
        "contract": stable_value(result.contract),
    }
    atomic_write_json(context.stage_dir / STAGE_RESULT_FILENAME, stage_result)
    success = {
        "schema_version": SCHEMA_VERSION,
        "stage": result.stage,
        "config_fingerprint": config_fingerprint,
        "input_fingerprint": inputs_fingerprint,
        "outputs": outputs,
    }
    success_path = context.stage_dir / SUCCESS_MARKER
    atomic_write_json(success_path, success)
    record.update({
        "status": StageStatus.COMPLETE.value,
        "config_fingerprint": config_fingerprint,
        "input_fingerprint": inputs_fingerprint,
        "outputs": outputs,
        "metrics": stage_result["metrics"],
        "contract": stage_result["contract"],
        "completed_at": utc_now(),
        "error": None,
    })
    manifest["status"] = StageStatus.COMPLETE.value
    try:
        _write_manifest(context, manifest)
    except BaseException as error:
        try:
            success_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            error.add_note(f"could not remove incomplete success marker: {cleanup_error}")
        raise


def end_stage_with_status(context: RunContext, status: StageStatus, error: str | None = None) -> None:
    if status not in {StageStatus.FAILED, StageStatus.INTERRUPTED}:
        raise ValueError("only failed/interrupted terminal states are accepted")
    manifest = verify_run_ownership(context)
    record = manifest["stages"].get(context.stage_name or "")
    if record is None or record["status"] != StageStatus.RUNNING.value:
        raise ValueError("stage is not running")
    record.update({"status": status.value, "error": error, "ended_at": utc_now()})
    manifest["status"] = status.value
    _write_manifest(context, manifest)


def evaluate_resume(
    context: RunContext,
    *,
    config_fingerprint: str,
    inputs_fingerprint: str,
    required_outputs: Mapping[str, Path],
    require_run_complete: bool = True,
) -> ResumeDecision:
    if context.stage_name is None or context.stage_dir is None:
        return ResumeDecision(False, ("stage context required",))
    reasons = []
    try:
        manifest = verify_run_ownership(context)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        return ResumeDecision(False, (str(error),))
    if require_run_complete and manifest.get("status") != StageStatus.COMPLETE.value:
        reasons.append("manifest run is not complete")
    record = manifest["stages"].get(context.stage_name)
    if record is None or record.get("status") != StageStatus.COMPLETE.value:
        reasons.append("manifest stage is not complete")
    stage_dir = context.stage_dir
    expected_directory = stage_dir.relative_to(context.run_dir).as_posix()
    if record is not None and record.get("directory") != expected_directory:
        reasons.append("manifest stage directory differs")
    if record is not None and record.get("config_fingerprint") != config_fingerprint:
        reasons.append("config fingerprint changed")
    if record is not None and record.get("input_fingerprint") != inputs_fingerprint:
        reasons.append("input fingerprint changed")
    if record is not None and record.get("contract", {}).get("status") != "passed":
        reasons.append("manifest contract is not passed")
    required_names = set(required_outputs)
    recorded_outputs = record.get("outputs", {}) if record else {}
    if not isinstance(recorded_outputs, dict) or set(recorded_outputs) != required_names:
        reasons.append("manifest output allowlist differs")
        recorded_outputs = recorded_outputs if isinstance(recorded_outputs, dict) else {}

    documents = {
        "success marker": stage_dir / SUCCESS_MARKER,
        "stage result": stage_dir / STAGE_RESULT_FILENAME,
    }
    loaded_documents: dict[str, dict[str, Any] | None] = {}
    for label, path in documents.items():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            value = None
            reasons.append(f"{label} missing or invalid")
        if not isinstance(value, dict):
            if value is not None:
                reasons.append(f"{label} is not an object")
            loaded_documents[label] = None
            continue
        loaded_documents[label] = value
        if value.get("schema_version") != SCHEMA_VERSION:
            reasons.append(f"{label} schema differs")
        if value.get("stage") != context.stage_name:
            reasons.append(f"{label} stage differs")
        if value.get("config_fingerprint") != config_fingerprint:
            reasons.append(f"{label} config fingerprint changed")
        if value.get("input_fingerprint") != inputs_fingerprint:
            reasons.append(f"{label} input fingerprint changed")
        document_outputs = value.get("outputs")
        if not isinstance(document_outputs, dict) or set(document_outputs) != required_names:
            reasons.append(f"{label} output allowlist differs")
        if label == "stage result" and value.get("status") != StageStatus.COMPLETE.value:
            reasons.append("stage result is not complete")
        if label == "stage result" and value.get("contract", {}).get("status") != "passed":
            reasons.append("stage result contract is not passed")

    for name, path in required_outputs.items():
        try:
            owned = assert_owned_path(path, stage_dir)
            actual = file_record(owned)
        except (FileNotFoundError, IsADirectoryError, OSError, ValueError) as error:
            reasons.append(f"required output {name!r} invalid: {error}")
            continue
        expected = recorded_outputs.get(name)
        if not isinstance(expected, dict) or expected.get("sha256") != actual["sha256"]:
            reasons.append(f"required output {name!r} hash mismatch")
        elif expected != actual:
            reasons.append(f"required output {name!r} record mismatch")
        for label, value in loaded_documents.items():
            document_outputs = value.get("outputs", {}) if value else {}
            expected = document_outputs.get(name)
            if not isinstance(expected, dict) or expected.get("sha256") != actual["sha256"]:
                reasons.append(f"{label} output {name!r} hash mismatch")
            elif expected != actual:
                reasons.append(f"{label} output {name!r} record mismatch")
    return ResumeDecision(not reasons, tuple(reasons))


def validate_resume_overwrite(*, resume: bool, overwrite: bool) -> None:
    if resume and overwrite:
        raise ValueError("resume and overwrite are mutually exclusive")


def assert_run_fingerprints(manifest: Mapping[str, Any], *, config_fingerprint: str, inputs_fingerprint: str) -> None:
    if manifest["config"]["fingerprint"] != config_fingerprint:
        raise ValueError("run config fingerprint changed; use a new run ID")
    if manifest["inputs"]["fingerprint"] != inputs_fingerprint:
        raise ValueError("run input fingerprint changed; use a new run ID")


def backup_manifest(context: RunContext) -> Path:
    source = context.run_dir / MANIFEST_FILENAME
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = context.run_dir / f"manifest.backup.{timestamp}.json"
    shutil.copy2(source, destination)
    return destination


def invalidate_from_stage(
    context: RunContext,
    *,
    ordered_stages: list[str],
    from_stage: str,
) -> Path:
    manifest = verify_run_ownership(context)
    if from_stage not in ordered_stages:
        raise ValueError(f"unknown stage: {from_stage}")
    backup = backup_manifest(context)
    for stage in ordered_stages[ordered_stages.index(from_stage):]:
        record = manifest["stages"].get(stage)
        if record is None:
            continue
        expected = context.for_stage(stage).stage_dir
        expected_directory = expected.relative_to(context.run_dir).as_posix()
        missing_clean_directory = (
            record.get("status") == StageStatus.NOT_STARTED.value
            and record.get("directory") is None
            and not expected.exists()
        )
        if record.get("directory") != expected_directory and not missing_clean_directory:
            raise ValueError(f"manifest does not own stage directory: {stage}")
        if expected.exists():
            owned = assert_owned_path(expected, context.run_dir)
            shutil.rmtree(owned)
        manifest["stages"][stage] = {
            "status": StageStatus.NOT_STARTED.value,
            "directory": expected_directory,
        }
    manifest["status"] = StageStatus.NOT_STARTED.value
    _write_manifest(context, manifest)
    return backup
