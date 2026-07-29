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
                if module == "lib" or module.startswith("lib.") or module == "src" or module.startswith("src."):
                    violations.append(f"{path}:{node.lineno}: from {module}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"insert", "append"} and isinstance(node.func.value, ast.Attribute):
                    if isinstance(node.func.value.value, ast.Name) and node.func.value.value.id == "sys" and node.func.value.attr == "path":
                        violations.append(f"{path}:{node.lineno}: sys.path.{node.func.attr}")
    assert violations == []


def test_zoning_layer_boundaries_and_regularized_import_graph() -> None:
    regularized_root = PACKAGE_ROOT / "zoning" / "regularized"
    graph = {}
    violations = []
    for path in sorted((PACKAGE_ROOT / "zoning").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_name = ".".join(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)
        graph.setdefault(module_name, set())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            imported = node.module or ""
            if imported.startswith("roadnet_partition.zoning.regularized") and path.is_relative_to(regularized_root):
                graph[module_name].add(imported)
            if path.name == "contracts.py" and imported.endswith(".cli"):
                violations.append(f"{path}:{node.lineno}: contracts imports CLI")
            if "algorithms" in path.parts and imported.startswith("roadnet_partition.pipeline"):
                violations.append(f"{path}:{node.lineno}: algorithm imports pipeline")
            if imported.startswith(("lib", "src", "stages", "adaptive_clustering")):
                violations.append(f"{path}:{node.lineno}: legacy import {imported}")

    visiting = set()
    visited = set()
    def visit(module: str) -> None:
        if module in visiting:
            violations.append(f"regularized import cycle at {module}")
            return
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph.get(module, ()):
            visit(dependency)
        visiting.remove(module)
        visited.add(module)
    for module in graph:
        visit(module)
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
        "roadnet_partition.downstream.demand",
        "roadnet_partition.downstream.supply",
        "roadnet_partition.downstream.supply_contracts",
        "roadnet_partition.graphs.build",
        "roadnet_partition.graphs.relations",
        "roadnet_partition.graphs.distance",
        "roadnet_partition.zoning.algorithms.common",
        "roadnet_partition.zoning.algorithms.leiden",
        "roadnet_partition.zoning.algorithms.region_growing",
        "roadnet_partition.zoning.partition",
        "roadnet_partition.zoning.contracts",
        "roadnet_partition.zoning.regularized.objective",
        "roadnet_partition.zoning.regularized.search",
        "roadnet_partition.zoning.regularized.selection",
        "roadnet_partition.zoning.metrics",
    ]
    command = [sys.executable, "-c", "; ".join(f"import {module}" for module in modules)]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_demand_package_boundaries_are_one_way() -> None:
    demand_path = PACKAGE_ROOT / "downstream" / "demand.py"
    contracts_path = PACKAGE_ROOT / "downstream" / "demand_contracts.py"
    graph_path = PACKAGE_ROOT / "graphs" / "build.py"
    geospatial_path = PACKAGE_ROOT / "io" / "geospatial.py"
    imports = {}
    for path in [demand_path, contracts_path, graph_path, geospatial_path]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports[path.name] = {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
    assert not any(module.endswith(("order_dataset", "stage2_demand")) for modules in imports.values() for module in modules)
    assert not any("supply" in module or "tte" in module for module in imports["demand.py"])
    assert not any(module.endswith(".cli") for module in imports["demand.py"] | imports["demand_contracts.py"])
    assert not any("downstream.demand" in module for module in imports["build.py"] | imports["geospatial.py"])
    assert not any("graphs.build" in module for module in imports["demand_contracts.py"])
