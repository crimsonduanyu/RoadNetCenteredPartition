from __future__ import annotations

import ast
from pathlib import Path

from roadnet_partition.pipeline.runner import resolve_pipeline_config
from roadnet_partition.pipeline.stages import canonical_partition_output_key, formal_stage_outputs


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/roadnet_partition"


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_pipeline_import_boundaries_and_fixed_order() -> None:
    runner = PACKAGE / "pipeline/runner.py"
    worker = PACKAGE / "pipeline/worker.py"
    stages = PACKAGE / "pipeline/stages.py"
    assert not imports(runner) & {"run_pipeline", "src.run_pipeline", "stages.stage1_partition", "stages.stage2_demand", "stages.stage3_supply"}
    assert "roadnet_partition.cli" not in imports(worker)
    assert "roadnet_partition.pipeline.runner" not in imports(stages)
    for path in [
        PACKAGE / "zoning/partition.py",
        PACKAGE / "downstream/demand.py",
        PACKAGE / "downstream/supply.py",
        PACKAGE / "downstream/tte.py",
        PACKAGE / "zoning/contracts.py",
        PACKAGE / "downstream/demand_contracts.py",
        PACKAGE / "downstream/supply_contracts.py",
        PACKAGE / "downstream/tte_contracts.py",
    ]:
        assert "roadnet_partition.pipeline.runner" not in imports(path)
    for path in [runner, worker, stages, PACKAGE / "pipeline/results.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module and node.module.split(".", 1)[0] in {"lib", "src"}
            for node in ast.walk(tree)
        )
        assert not any(
            isinstance(node, ast.Call) and ast.unparse(node.func).startswith("sys.path.")
            for node in ast.walk(tree)
        )
    tree = ast.parse(runner.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.keyword) and node.arg == "shell" and isinstance(node.value, ast.Constant) and node.value.value is True
        for node in ast.walk(tree)
    )
    assert "DAG" not in runner.read_text(encoding="utf-8")
    assert "sys.executable" in runner.read_text(encoding="utf-8")
    assert "roadnet-partition partition" not in runner.read_text(encoding="utf-8")
    assert "execute_pipeline_stage" not in "\n".join(
        path.read_text(encoding="utf-8") for path in [runner, worker, stages]
    )


def test_production_pipeline_config_resolves_stably_without_running() -> None:
    path = ROOT / "configs/pipelines/full.yaml"
    first = resolve_pipeline_config(path)
    second = resolve_pipeline_config(path)
    assert first.fingerprint == second.fingerprint
    assert tuple(first.stages) == ("partition", "demand", "supply", "tte")
    assert all(stage.scope == first.scope for stage in first.stages.values())
    assert all(stage.source_path.is_file() for stage in first.stages.values())
    assert first.run_root == ROOT / "outputs/runs"
    partition_outputs = formal_stage_outputs("partition", first.stages["partition"], ROOT / "outputs/runs/read-only/partition")
    assert canonical_partition_output_key(partition_outputs).startswith("cluster_gpkg_")
    demand_inputs = first.stages["demand"].values["order_pipeline"]["inputs"]
    tte_inputs = first.stages["tte"].values["stage4_tte"]["inputs"]
    checked_paths = [
        *[Path(value) for key, value in first.stages["partition"].values["inputs"].items() if key != "baseline_clusters" and isinstance(value, str)],
        *[Path(value) for value in first.stages["partition"].values["inputs"]["baseline_clusters"].values()],
        Path(demand_inputs["road_relation_edges_csv"]),
        *[Path(value) for value in demand_inputs["order_datasets"]],
        Path(demand_inputs["poi_path"]),
        Path(tte_inputs["network_distance_path"]),
        Path(tte_inputs["representative_nodes_path"]),
    ]
    assert all(path.is_file() for path in checked_paths)
