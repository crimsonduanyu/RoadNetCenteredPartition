from __future__ import annotations

import hashlib
import os
from pathlib import Path
import socket
import subprocess

import pytest

from roadnet_partition.config import config_fingerprint
from roadnet_partition.io import manifests


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repo / "tracked.bin").write_bytes(b"base")
    _git(repo, "add", ".gitignore", "tracked.bin")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_runtime_provenance_has_every_audited_distribution_and_stable_digest() -> None:
    first = manifests.collect_runtime_info()
    second = manifests.collect_runtime_info()
    manifests.validate_runtime_provenance(first)
    expected = {manifests._normalized_distribution_name(item[0]) for item in manifests.RESULT_AFFECTING_DISTRIBUTIONS}
    records = first["distributions"]
    assert {record["normalized_name"] for record in records} == expected
    assert any(record["name"] == "python-igraph" and record["import_aliases"] == ["igraph"] for record in records)
    assert any(record["name"] == "python-louvain" and record["import_aliases"] == ["community", "community.community_louvain"] for record in records)
    assert first == second
    assert not any("environment" in record for record in records)


def test_missing_distribution_is_explicit_without_importing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    original = manifests.importlib.metadata.version

    def version(name: str) -> str:
        if name == "osmnx":
            raise manifests.importlib.metadata.PackageNotFoundError(name)
        return original(name)

    monkeypatch.setattr(manifests.importlib.metadata, "version", version)
    record = next(item for item in manifests.collect_runtime_info()["distributions"] if item["name"] == "osmnx")
    assert record["status"] == "unavailable"
    assert record["version"] is None
    assert record["unavailable_reason"] == "distribution_not_installed"


def test_distribution_versions_use_metadata_without_package_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        manifests.importlib, "import_module",
        lambda name: (_ for _ in ()).throw(AssertionError(f"imported {name}")),
    )
    assert len(manifests._distribution_records()) == len(manifests.RESULT_AFFECTING_DISTRIBUTIONS)


def test_native_records_are_complete_path_free_and_digest_valid() -> None:
    runtime = manifests.collect_runtime_info()
    manifests.validate_runtime_provenance(runtime)
    assert set(runtime["native_libraries"]) == set(manifests.NATIVE_LIBRARY_NAMES)
    for record in runtime["native_libraries"].values():
        for source in record["sources"]:
            assert source["value"] is None or ("/" not in source["value"] and "\\" not in source["value"])


def test_untracked_bytes_not_status_text_determine_git_digest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = repo / "same.bin"
    path.write_bytes(b"AAAA")
    first = manifests.collect_git_info(repo)
    path.write_bytes(b"BBBB")
    second = manifests.collect_git_info(repo)
    manifests.validate_git_provenance(first)
    manifests.validate_git_provenance(second)
    assert first["status"] == second["status"]
    assert first["untracked_files"][0]["size"] == second["untracked_files"][0]["size"]
    assert first["untracked_files"][0]["sha256"] != second["untracked_files"][0]["sha256"]
    assert first["digest"] != second["digest"]
    base = {"config": "same", "git": first["digest"]}
    changed = {"config": "same", "git": second["digest"]}
    assert config_fingerprint(base) != config_fingerprint(changed)


def test_untracked_empty_binary_unicode_nested_mode_and_mtime(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "empty").write_bytes(b"")
    (repo / "binary").write_bytes(bytes(range(256)))
    (repo / "子目录").mkdir()
    executable = repo / "子目录" / "脚本"
    executable.write_bytes(b"x")
    executable.chmod(0o755)
    first = manifests.collect_git_info(repo)
    os.utime(executable, None)
    second = manifests.collect_git_info(repo)
    assert first["digest"] == second["digest"]
    records = {record["path"]: record for record in first["untracked_files"]}
    assert records["empty"]["sha256"] == hashlib.sha256(b"").hexdigest()
    assert records["子目录/脚本"]["executable"] is True
    executable.chmod(0o644)
    assert manifests.collect_git_info(repo)["digest"] != first["digest"]


def test_ignored_bytes_never_enter_git_provenance(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ignored = repo / "ignored" / "private.bin"
    ignored.parent.mkdir()
    ignored.write_bytes(b"secret-a")
    first = manifests.collect_git_info(repo)
    ignored.write_bytes(b"secret-b")
    second = manifests.collect_git_info(repo)
    assert first == second
    assert not any(record["path"].startswith("ignored/") for record in first["untracked_files"])


@pytest.mark.parametrize("kind", ["symlink", "fifo", "socket"])
def test_untracked_special_types_are_rejected_without_following(tmp_path: Path, kind: str) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "outside-marker"
    outside.write_bytes(b"unchanged")
    special = repo / "special"
    listener = None
    if kind == "symlink":
        special.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(special)
    else:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(special))
    try:
        with pytest.raises(manifests.ProvenanceError, match="git_untracked_unsupported_type"):
            (manifests.collect_git_info(repo) if kind == "symlink" else manifests._untracked_file_record(repo, "special"))
        assert outside.read_bytes() == b"unchanged"
    finally:
        if listener is not None:
            listener.close()


def test_tracked_diff_covers_unstaged_staged_binary_delete_rename_and_mode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    clean = manifests.collect_git_info(repo)
    states = []
    (repo / "tracked.bin").write_bytes(b"changed")
    states.append(manifests.collect_git_info(repo)["tracked_diff_sha256"])
    _git(repo, "add", "tracked.bin")
    states.append(manifests.collect_git_info(repo)["tracked_diff_sha256"])
    (repo / "tracked.bin").write_bytes(b"\x00\xffbinary")
    states.append(manifests.collect_git_info(repo)["tracked_diff_sha256"])
    _git(repo, "mv", "tracked.bin", "renamed.bin")
    states.append(manifests.collect_git_info(repo)["tracked_diff_sha256"])
    (repo / "renamed.bin").chmod(0o755)
    states.append(manifests.collect_git_info(repo)["tracked_diff_sha256"])
    (repo / "renamed.bin").unlink()
    states.append(manifests.collect_git_info(repo)["tracked_diff_sha256"])
    assert clean["clean"] is True
    assert all(value != clean["tracked_diff_sha256"] for value in states)


def test_manifest_validators_reject_tamper_duplicates_and_bad_order() -> None:
    runtime = manifests.collect_runtime_info()
    runtime["distributions"].append(dict(runtime["distributions"][0]))
    runtime["digest"] = manifests._canonical_digest(runtime)
    with pytest.raises(ValueError, match="unique and sorted"):
        manifests.validate_runtime_provenance(runtime)


def test_untracked_hash_rejects_change_during_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    path = repo / "racing.bin"
    path.write_bytes(b"a" * 32)
    original = manifests.os.read
    changed = False

    def read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        block = original(descriptor, size)
        if block and not changed and os.fstat(descriptor).st_ino == path.stat().st_ino:
            changed = True
            path.write_bytes(b"b" * 32)
            current = path.stat()
            os.utime(path, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000))
        return block

    monkeypatch.setattr(manifests.os, "read", read)
    with pytest.raises(manifests.ProvenanceError, match="git_untracked_content_changed"):
        manifests.collect_git_info(repo)


def test_untracked_hash_rejects_deleted_or_symlink_replacement(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = repo / "replace-me"
    path.write_bytes(b"inside")
    path.unlink()
    with pytest.raises(manifests.ProvenanceError, match="git_untracked_content_changed"):
        manifests._untracked_file_record(repo, "replace-me")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside marker")
    path.symlink_to(outside)
    with pytest.raises(manifests.ProvenanceError, match="git_untracked_unsupported_type"):
        manifests._untracked_file_record(repo, "replace-me")
    assert outside.read_bytes() == b"outside marker"


def test_provenance_mismatch_reason_codes_are_field_specific() -> None:
    runtime = manifests.collect_runtime_info()
    changed = {**runtime, "platform": {**runtime["platform"], "machine": "synthetic"}}
    changed["digest"] = manifests._canonical_digest(changed)
    git = {
        "schema_version": 2, "repository_status": "unavailable", "complete": False,
        "commit": None, "branch": None, "detached": None, "clean": None, "dirty": None,
        "status": [], "tracked_diff_sha256": None, "untracked_files": [],
        "changed_files": [], "diff_sha256": None,
    }
    git["digest"] = manifests._canonical_digest(git)
    reasons = manifests.provenance_mismatch_reasons(runtime, changed, git, git)
    assert reasons == {
        "runtime": ["runtime_platform_changed"],
        "git": ["git_provenance_match"],
        "compatible": False,
    }
    native_changed = manifests.stable_value(runtime)
    native_changed["native_libraries"]["sqlite"]["sources"][0]["value"] = "0.0-synthetic"
    native_changed["digest"] = manifests._canonical_digest(native_changed)
    native_reasons = manifests.provenance_mismatch_reasons(runtime, native_changed, git, git)
    assert native_reasons["runtime"] == ["runtime_native_library_changed"]
    assert config_fingerprint({"runtime": runtime["digest"]}) != config_fingerprint({"runtime": native_changed["digest"]})
