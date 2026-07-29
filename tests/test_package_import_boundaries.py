from __future__ import annotations

import ast
import importlib
from pathlib import Path
import pkgutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src/roadnet_partition"


def test_new_package_has_no_legacy_imports_or_path_injection() -> None:
    violations = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "lib" or alias.name.startswith("lib."):
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "lib" or module.startswith("lib."):
                    violations.append(f"{path}:{node.lineno}: from {module}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"insert", "append"} and isinstance(node.func.value, ast.Attribute):
                    if isinstance(node.func.value.value, ast.Name) and node.func.value.value.id == "sys" and node.func.value.attr == "path":
                        violations.append(f"{path}:{node.lineno}: sys.path.{node.func.attr}")
    assert violations == []


def test_every_new_package_module_imports_without_cycles() -> None:
    import roadnet_partition

    modules = sorted(
        item.name for item in pkgutil.walk_packages(roadnet_partition.__path__, "roadnet_partition.")
    )
    for name in modules:
        importlib.import_module(name)


def test_migrated_modules_import_from_outside_repository(tmp_path: Path) -> None:
    modules = [
        "roadnet_partition.io.geospatial",
        "roadnet_partition.graphs.relations",
        "roadnet_partition.graphs.distance",
        "roadnet_partition.zoning.algorithms.common",
        "roadnet_partition.zoning.metrics",
    ]
    command = [sys.executable, "-c", "; ".join(f"import {module}" for module in modules)]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
