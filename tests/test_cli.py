from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    return environment


def test_distribution_registers_console_script() -> None:
    distribution = importlib.metadata.distribution("roadnet-partition")
    scripts = {
        entry.name: entry.value
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }
    assert scripts["roadnet-partition"] == "roadnet_partition.cli:main"


def test_module_entrypoint_shows_help_outside_repository(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "roadnet_partition"],
        cwd=tmp_path,
        env=clean_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "usage: roadnet-partition" in result.stdout


def test_module_entrypoint_reports_version_outside_repository(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "roadnet_partition", "--version"],
        cwd=tmp_path,
        env=clean_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"roadnet-partition {importlib.metadata.version('roadnet-partition')}"


def test_editable_package_imports_outside_repository(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import roadnet_partition; print(roadnet_partition.__version__); print(roadnet_partition.__file__)",
        ],
        cwd=tmp_path,
        env=clean_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    version, module_path = result.stdout.strip().splitlines()
    assert version == importlib.metadata.version("roadnet-partition")
    assert Path(module_path).resolve() == PROJECT_ROOT / "src/roadnet_partition/__init__.py"


@pytest.mark.parametrize("stage", [None, "partition", "demand", "supply", "tte"])
def test_console_help_lists_only_phase6a_commands_outside_repository(tmp_path: Path, stage: str | None) -> None:
    executable = shutil.which("roadnet-partition")
    assert executable is not None
    command = [executable, "--help"] if stage is None else [executable, stage, "--help"]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=clean_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "usage: roadnet-partition" in result.stdout
    if stage is None:
        assert (
            "{check-raw,run,validate,publish,export-reproduction,"
            "migrate-legacy-graph,partition,demand,supply,tte}"
        ) in result.stdout
    else:
        for option in ["--config", "--run-id", "--run-dir", "--resume", "--overwrite"]:
            assert option in result.stdout
        assert ("--n-blocks" in result.stdout) is (stage == "supply")
        # Opting in to pickle deserialization must never ride along with a stage.
        assert "--allow-trusted-legacy-graph-pickle" not in result.stdout
