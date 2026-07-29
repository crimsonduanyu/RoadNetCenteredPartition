from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from roadnet_partition.config import config_fingerprint, load_stage_config, stable_value
from roadnet_partition.io.paths import UnsafePathError, assert_owned_path, assert_safe_run_dir, resolve_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_config_paths_resolve_from_config_directory_not_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config = config_dir / "stage.yaml"
    config.write_text(
        "project_root: ..\ninputs:\n  source: data/input.csv\n  sources: [a.csv, ~/b.csv]\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = load_stage_config(
        config,
        path_fields=("inputs.source", "inputs.sources"),
    )

    assert resolved.values["project_root"] == tmp_path.resolve()
    assert resolved.values["inputs"]["source"] == (config_dir / "data/input.csv").resolve()
    assert resolved.values["inputs"]["sources"][0] == (config_dir / "a.csv").resolve()
    assert resolved.values["inputs"]["sources"][1] == Path("~/b.csv").expanduser().resolve()


def test_fingerprint_ignores_yaml_key_order_and_comments(tmp_path: Path) -> None:
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("a: 1\nb: [2, 3]\n", encoding="utf-8")
    second.write_text("# comment\nb: [2, 3]\na: 1\n", encoding="utf-8")
    assert load_stage_config(first).fingerprint == load_stage_config(second).fingerprint


def test_stable_value_serializes_paths_and_rejects_unknown_types(tmp_path: Path) -> None:
    assert stable_value({"path": tmp_path, "items": (2, 1)}) == {
        "items": [2, 1],
        "path": tmp_path.as_posix(),
    }
    with pytest.raises(TypeError):
        config_fingerprint({"bad": object()})


def test_resolve_path_requires_explicit_base_and_supports_absolute_paths(tmp_path: Path) -> None:
    assert resolve_path("child", base_dir=tmp_path) == (tmp_path / "child").resolve()
    assert resolve_path(tmp_path / "absolute", base_dir=Path("/")) == (tmp_path / "absolute").resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific foreign path check")
@pytest.mark.parametrize("value", [r"..\data\file.csv", Path(r"data\file.csv")])
def test_resolve_path_rejects_windows_separators_on_posix(tmp_path: Path, value: str | Path) -> None:
    with pytest.raises(ValueError, match="Windows path"):
        resolve_path(value, base_dir=tmp_path)


def test_safe_run_directory_rejects_protected_roots_and_allows_external(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for relative in [
        ".", "data", "data/raw", "data/interim", "data/processed/x",
        "artifacts/golden/v1", "Golden/v1",
        "golden/v1", "release/v1", "releases/v1",
    ]:
        with pytest.raises(UnsafePathError):
            assert_safe_run_dir(project / relative, project)
    assert assert_safe_run_dir(tmp_path / "external-runs/run-1", project) == (tmp_path / "external-runs/run-1").resolve()

    target = tmp_path / "real-run"
    target.mkdir()
    link = tmp_path / "run-link"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(UnsafePathError, match="symbolic link"):
        assert_safe_run_dir(link, project)

    configured_data = tmp_path / "external-data"
    with pytest.raises(UnsafePathError):
        assert_safe_run_dir(
            configured_data / "processed/run",
            project,
            protected_roots=(configured_data,),
        )


def test_owned_path_rejects_escape_owner_and_symlink(tmp_path: Path) -> None:
    owner = tmp_path / "run"
    owner.mkdir()
    assert assert_owned_path(owner / "partition", owner) == owner / "partition"
    with pytest.raises(UnsafePathError):
        assert_owned_path(owner, owner)
    with pytest.raises(UnsafePathError):
        assert_owned_path(owner / "../outside", owner)

    outside = tmp_path / "outside"
    outside.mkdir()
    link = owner / "link"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(UnsafePathError):
        assert_owned_path(link / "child", owner)

    inside = owner / "inside"
    inside.mkdir()
    internal_link = owner / "internal-link"
    os.symlink(inside, internal_link)
    with pytest.raises(UnsafePathError):
        assert_owned_path(internal_link / "child", owner)


def test_config_key_map_covers_every_current_mapping_key_once() -> None:
    import yaml

    config = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    key_map = json.loads((PROJECT_ROOT / "docs/refactor/config-key-map-v1.json").read_text(encoding="utf-8"))

    def mapping_keys(value: object, prefix: tuple[str, ...] = ()) -> list[str]:
        paths = []
        if isinstance(value, dict):
            for key, child in value.items():
                path = prefix + (str(key),)
                paths.append(".".join(path))
                paths.extend(mapping_keys(child, path))
        return paths

    expected = mapping_keys(config)
    actual = [entry["key_path"] for entry in key_map["entries"]]
    assert key_map["mapping_key_count"] == 341
    assert len(actual) == len(set(actual))
    assert sorted(actual) == sorted(expected)
    migrated_legacy_readers = {
        "src/lib/geo.py", "src/lib/graph.py", "src/lib/clustering.py",
        "src/lib/network_distance.py", "src/lib/metrics.py",
    }
    assert not {
        reader["file"]
        for entry in key_map["entries"]
        for reader in entry["readers"]
    } & migrated_legacy_readers
