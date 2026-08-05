from __future__ import annotations

import argparse
import ast
import importlib
from pathlib import Path
import pkgutil
import re
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src/roadnet_partition"
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"

#: Modules that turn bytes on disk into live Python objects.
EXECUTABLE_DESERIALIZERS = {"pickle", "marshal", "shelve", "dill", "cloudpickle", "joblib"}


def _shipped_sources() -> list[Path]:
    """Every Python file we ship: the package plus the operator scripts."""

    return sorted(PACKAGE_ROOT.rglob("*.py")) + sorted(SCRIPTS_ROOT.rglob("*.py"))


def test_shipped_code_never_imports_an_executable_deserializer() -> None:
    """AUD-005 / R5.2: no import of pickle & friends anywhere we ship.

    R5.1 allowed exactly one module to import ``pickle`` for trusted-only
    conversion. R5.2 deleted it, so the allowance is gone too — there is no
    skip list here on purpose. An import is the cheapest way this could
    regress, so it is the thing to pin.
    """

    offenders = []
    for path in _shipped_sources():
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".")[0] in EXECUTABLE_DESERIALIZERS for alias in node.names):
                    offenders.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in EXECUTABLE_DESERIALIZERS:
                    offenders.append(f"{relative}:{node.lineno}")
    assert not offenders, sorted(set(offenders))


#: Dynamic imports whose module name is not a literal, reviewed once.
#: ``manifests._native_source`` probes native library versions and every one of
#: its call sites passes a hard-coded name (duckdb, fiona, shapely, ...), so it
#: cannot reach a deserializer. A new entry here needs the same argument.
REVIEWED_DYNAMIC_IMPORTS = {"src/roadnet_partition/io/manifests.py:178"}


def test_shipped_code_cannot_reach_a_deserializer_dynamically() -> None:
    """A static import scan is worthless if ``__import__("pickle")`` still works."""

    offenders = []
    for path in _shipped_sources():
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func) not in {"__import__", "importlib.import_module", "import_module"}:
                continue
            site = f"{relative}:{node.lineno}"
            if node.args and isinstance(node.args[0], ast.Constant):
                if str(node.args[0].value).split(".")[0] in EXECUTABLE_DESERIALIZERS:
                    offenders.append(site)
            elif site not in REVIEWED_DYNAMIC_IMPORTS:
                offenders.append(f"{site} (unreviewed non-literal dynamic import)")
    assert not offenders, sorted(set(offenders))


def test_shipped_code_contains_no_deserialization_call_or_legacy_symbol() -> None:
    """Text-level backstop for the R5.1 surface and for pickle call sites."""

    forbidden = (
        "pickle.load",
        "pickle.loads",
        "pickle.dump",
        "pickle.dumps",
        "pickle.Unpickler",
        "read_gpickle",
        "write_gpickle",
        "marshal.load",
        "trusted_legacy_graph_pickle",
        "LegacyGraphDeclaration",
        "allow-trusted-legacy",
        "allow_trusted_legacy",
        "migrate-legacy-graph",
    )
    offenders = []
    for path in _shipped_sources():
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        offenders.extend(f"{relative}: {token}" for token in forbidden if token in source)
    assert not offenders, sorted(offenders)


def test_no_legacy_opt_in_survives_on_any_cli_subcommand() -> None:
    """The R5.1 opt-in must be gone, not merely hidden from one subcommand."""

    from roadnet_partition import cli

    parser = cli.build_parser()
    assert "allow_trusted_legacy_graph_pickle" not in {action.dest for action in parser._actions}

    registered = set(next(
        action.choices for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ))
    assert "migrate-legacy-graph" not in registered

    for command, extra in (
        ("run", ["--config", "c.yaml"]),
        ("partition", ["--config", "c.yaml"]),
        ("demand", ["--config", "c.yaml"]),
        ("supply", ["--config", "c.yaml"]),
        ("tte", ["--config", "c.yaml"]),
        ("publish", ["--run", "r", "--scope", "s"]),
        ("export-reproduction", ["--run", "r", "--output", "o"]),
    ):
        namespace = parser.parse_args([command, *extra])
        assert not hasattr(namespace, "allow_trusted_legacy_graph_pickle"), command
        with pytest.raises(SystemExit):
            parser.parse_args([command, *extra, "--allow-trusted-legacy-graph-pickle"])


def test_repository_has_no_active_legacy_execution_references() -> None:
    allowed = {
        "configs/legacy/config.pre-refactor.yaml",
        "docs/history/refactor-v1.md",
    }
    patterns = (
        "IntermediateDataFor" + "Reproduce",
        "src/run_" + "pipeline.py",
        "src/" + "stages",
        "sys.path." + "insert",
        "sys.path." + "append",
    )
    legacy_import = re.compile(r"(?:from|import)\s+(?:li" + r"b|sr" + r"c)(?:\.|\s|$)")
    suffixes = {".py", ".md", ".json", ".yaml", ".yml", ".txt", ".sh", ".toml", ".sha256"}
    violations = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in {".git", ".conda", ".venv", "venv", "env"} for part in path.parts):
            continue
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative.startswith(("outputs/", "data/")):
            continue
        if relative in allowed:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(pattern in text for pattern in patterns) or legacy_import.search(text):
            violations.append(relative)
    assert not violations, "\n".join(violations)


def test_new_package_has_no_legacy_imports_or_path_injection() -> None:
    violations = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if (
                        alias.name == "lib"
                        or alias.name.startswith("lib.")
                        or alias.name == "src"
                        or alias.name.startswith("src.")
                    ):
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
        "roadnet_partition.downstream.tte",
        "roadnet_partition.downstream.tte_contracts",
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


def test_tte_package_boundaries_are_one_way_and_nonserialized() -> None:
    paths = {
        "tte": PACKAGE_ROOT / "downstream" / "tte.py",
        "contracts": PACKAGE_ROOT / "downstream" / "tte_contracts.py",
        "distance": PACKAGE_ROOT / "graphs" / "distance.py",
    }
    trees = {
        name: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, path in paths.items()
    }

    def imported_names(tree: ast.AST) -> set[str]:
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names.update(
                    module if alias.name == "*" else f"{module}.{alias.name}"
                    for alias in node.names
                )
        return names

    imports = {name: imported_names(tree) for name, tree in trees.items()}
    forbidden_tte = (
        "lib",
        "src",
        "stages",
        "roadnet_partition.downstream.demand",
        "roadnet_partition.downstream.supply",
        "roadnet_partition.cli",
    )
    assert not any(
        imported == prefix or imported.startswith(f"{prefix}.")
        for imported in imports["tte"]
        for prefix in forbidden_tte
    )
    assert not any(
        imported.startswith("roadnet_partition.pipeline")
        or imported.startswith("roadnet_partition.cli")
        for imported in imports["contracts"]
    )
    assert any(
        imported.startswith("roadnet_partition.graphs.distance")
        for imported in imports["tte"]
    )
    assert not any(
        imported.startswith("roadnet_partition.downstream.tte")
        for imported in imports["distance"]
    )

    forbidden_dynamic_modules = {"pickle", "joblib", "cloudpickle", "dill", "importlib"}
    for name, tree in trees.items():
        assert not any(
            imported.split(".", 1)[0] in forbidden_dynamic_modules
            for imported in imports[name]
        )
        assert not any(
            isinstance(node, ast.Call) and ast.unparse(node.func) == "__import__"
            for node in ast.walk(tree)
        )
        assert not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"__reduce__", "__reduce_ex__", "__getstate__", "__setstate__"}
            for node in ast.walk(tree)
        )
