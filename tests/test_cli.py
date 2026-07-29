from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys


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
    assert result.stdout.strip() == "roadnet-partition 0.1.0"
