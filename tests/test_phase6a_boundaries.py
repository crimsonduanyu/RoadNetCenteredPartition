from __future__ import annotations

import argparse
import ast
from pathlib import Path

from roadnet_partition.cli import build_parser


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "roadnet_partition"
ALLOWED_STAGES = {"partition", "demand", "supply", "tte"}
FORBIDDEN_COMMANDS = {"run", "publish", "validate", "export-reproduction"}
# One-off operator tools that execute no stage. `migrate-legacy-graph` is the
# single opt-in door to the pre-AUD-005 pickle format (see Gate E).
MAINTENANCE_COMMANDS = {"migrate-legacy-graph"}


def parse(relative: str) -> ast.Module:
    path = PROJECT_ROOT / relative
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imports(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def application_imports(tree: ast.AST) -> set[str]:
    return {
        name for name in imports(tree)
        if name.split(".", 1)[0] in {"roadnet_partition", "lib", "src", "stages", "run_pipeline"}
    }


def test_cli_and_stage_executor_only_depend_on_the_new_package() -> None:
    cli_imports = application_imports(parse("src/roadnet_partition/cli.py"))
    executor_imports = application_imports(parse("src/roadnet_partition/pipeline/stages.py"))

    assert cli_imports and all(name.startswith("roadnet_partition") for name in cli_imports)
    assert executor_imports and all(name.startswith("roadnet_partition") for name in executor_imports)
    assert not any(".cli" in name for name in executor_imports)
    assert not any("run_pipeline" in name for name in executor_imports)


def test_business_stages_and_contracts_do_not_import_cli() -> None:
    paths = [
        "src/roadnet_partition/zoning/partition.py",
        "src/roadnet_partition/zoning/contracts.py",
        "src/roadnet_partition/downstream/demand.py",
        "src/roadnet_partition/downstream/demand_contracts.py",
        "src/roadnet_partition/downstream/supply.py",
        "src/roadnet_partition/downstream/supply_contracts.py",
        "src/roadnet_partition/downstream/tte.py",
        "src/roadnet_partition/downstream/tte_contracts.py",
    ]
    violations = {
        relative: sorted(name for name in imports(parse(relative)) if name.endswith(".cli"))
        for relative in paths
    }
    assert not {path: names for path, names in violations.items() if names}


def test_public_cli_registers_phase6a_stages_and_phase6b_run() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    registered = set(subparsers.choices)

    assert registered == ALLOWED_STAGES | FORBIDDEN_COMMANDS | MAINTENANCE_COMMANDS | {"check-raw"}


def test_stage_executor_has_dispatch_but_no_subprocess_or_stage_sequence() -> None:
    tree = parse("src/roadnet_partition/pipeline/stages.py")
    assert "subprocess" not in imports(tree)
    assert not any(
        isinstance(node, ast.Call) and dotted_name(node.func).startswith("subprocess.")
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "run_pipeline.py" in node.value
        for node in ast.walk(tree)
    )

    execute = function(tree, "execute_stage")
    dispatch = function(tree, "_run_stage")
    assert not any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(execute))
    assert not any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(dispatch))
    assert sum(
        isinstance(node, ast.Call) and dotted_name(node.func) == "_run_stage"
        for node in ast.walk(execute)
    ) == 1
    assert sum(
        isinstance(node, ast.Call) and dotted_name(node.func) == "_run_stage"
        for node in ast.walk(tree)
    ) == 1
    business_calls = {"run_partition", "run_demand", "run_supply", "run_tte"}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name == "_run_stage":
            continue
        called = {
            dotted_name(item.func).rsplit(".", 1)[-1]
            for item in ast.walk(node)
            if isinstance(item, ast.Call)
        }
        assert called.isdisjoint(business_calls | {"execute_stage"})


def test_manifest_and_stdout_paths_do_not_read_environment_or_emit_details() -> None:
    trees = {
        name: parse(path)
        for name, path in {
            "manifest": "src/roadnet_partition/io/manifests.py",
            "executor": "src/roadnet_partition/pipeline/stages.py",
            "cli": "src/roadnet_partition/cli.py",
        }.items()
    }
    for tree in trees.values():
        assert not any(
            dotted_name(node) == "os.environ"
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
        )
        assert not any(
            isinstance(node, ast.Call)
            and dotted_name(node.func) in {"os.getenv", "os.environ.get"}
            for node in ast.walk(tree)
        )

    for channel in (trees["manifest"], trees["executor"]):
        assert not any(
            isinstance(node, ast.Call) and dotted_name(node.func) == "print"
            for node in ast.walk(channel)
        )

    forbidden_stdout_names = {"config", "inputs", "manifest", "outputs", "values"}
    for node in ast.walk(trees["cli"]):
        if not isinstance(node, ast.Call) or dotted_name(node.func) != "print":
            continue
        referenced = {
            child.id for argument in node.args for child in ast.walk(argument)
            if isinstance(child, ast.Name)
        }
        assert referenced.isdisjoint(forbidden_stdout_names)

    sensitive_fragments = {
        "order_id", "driver_id", "coordinate", "latitude", "longitude",
        "api_key", "credential", "secret", "proxy", "environment", "content", "sample",
    }
    for node in trees["executor"].body:
        if not isinstance(node, ast.FunctionDef) or not node.name.endswith("_contract"):
            continue
        for returned in (
            item.value for item in ast.walk(node)
            if isinstance(item, ast.Return) and isinstance(item.value, ast.Dict)
        ):
            keys = {
                key.value for key in returned.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            assert not {
                key for key in keys
                if any(fragment in key.lower() for fragment in sensitive_fragments)
            }
