from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/roadnet_partition"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_phase7_package_boundaries() -> None:
    validation = PACKAGE / "pipeline/validation.py"
    publishing = PACKAGE / "pipeline/publishing.py"
    reproduction = PACKAGE / "releases/reproduction.py"
    assert "execute_stage" not in validation.read_text(encoding="utf-8")
    assert not any(name.startswith("roadnet_partition.downstream") or name.startswith("roadnet_partition.zoning") for name in imported_modules(publishing))
    assert not any(name.startswith("roadnet_partition.downstream") or name.startswith("roadnet_partition.zoning") for name in imported_modules(reproduction))
    assert "transactional_scope_swap" in publishing.read_text(encoding="utf-8")
    assert "transactional_scope_swap" in reproduction.read_text(encoding="utf-8")
    for path in (validation, publishing, reproduction, PACKAGE / "releases/__init__.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module and node.module.split(".", 1)[0] in {"lib", "src"}
            for node in ast.walk(tree)
        )
        assert not any(
            isinstance(node, ast.Call) and ast.unparse(node.func).startswith("sys.path.")
            for node in ast.walk(tree)
        )
        assert "shell=True" not in path.read_text(encoding="utf-8")


def test_release_code_has_no_git_writes_or_registry_abstraction() -> None:
    text = "\n".join(
        (PACKAGE / relative).read_text(encoding="utf-8")
        for relative in ("pipeline/publishing.py", "releases/reproduction.py")
    )
    assert not any(command in text for command in ("git add", "git commit", "git lfs"))
    assert "artifact_registry" not in text.lower()
    assert "remote storage" not in text.lower()
    assert set(("public", "synthetic", "private", "restricted", "unknown")) <= set(text.split('"'))
