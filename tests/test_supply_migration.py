from __future__ import annotations

import builtins
import ast
from pathlib import Path

import pandas as pd
import pytest

from roadnet_partition.config import ResolvedStageConfig
from roadnet_partition.downstream import supply
from roadnet_partition.downstream.supply_contracts import TABLES, validate_supply_outputs
from roadnet_partition.pipeline.results import RunContext, StageStatus


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAL_FILES = {
    "supply_inservice_od.csv.gz",
    "supply_available_floor.csv.gz",
    "supply_fleet_lower_bound.csv.gz",
    "run_summary.json",
    "config_used.json",
}
def test_supply_import_boundaries_are_one_way() -> None:
    package_root = PROJECT_ROOT / "src/roadnet_partition"
    supply_tree = ast.parse((package_root / "downstream/supply.py").read_text(encoding="utf-8"))
    contract_tree = ast.parse((package_root / "downstream/supply_contracts.py").read_text(encoding="utf-8"))
    supply_imports = {node.module or "" for node in ast.walk(supply_tree) if isinstance(node, ast.ImportFrom)}
    contract_imports = {node.module or "" for node in ast.walk(contract_tree) if isinstance(node, ast.ImportFrom)}
    assert not any(module.startswith(("lib", "src", "stages")) for module in supply_imports | contract_imports)
    assert not any("demand" in module or "tte" in module or module.endswith(".cli") for module in supply_imports)
    assert not any(module.startswith("roadnet_partition.pipeline") for module in contract_imports)


def synthetic_orders() -> pd.DataFrame:
    rows = [
        (1, 101, "2017-06-01 23:40:00", "2017-06-01 23:55:00", 10, 30, "exclusive"),
        (2, 101, "2017-06-02 00:10:00", "2017-06-02 00:25:00", 30, 10, "exclusive"),
        (3, 202, "2017-06-01 08:00:00", "2017-06-01 08:30:00", 10, 50, "carpool"),
        (4, 202, "2017-06-01 08:10:00", "2017-06-01 08:40:00", 30, 70, "carpool"),
        (5, 303, "2017-06-01 09:00:00", "2017-06-01 09:15:00", 70, 70, "exclusive"),
        (6, 404, "2017-06-01 10:00:00", "2017-06-01 10:00:00", 50, 10, "exclusive"),
        (7, 505, "2017-06-01 11:20:00", "2017-06-01 11:20:00", 50, 30, "exclusive"),
    ]
    return pd.DataFrame(
        rows,
        columns=[
            "order_id", "driver_id", "departure_time", "finish_time",
            "origin_cluster_id", "destination_cluster_id", "service_type",
        ],
    )


def write_orders(path: Path) -> None:
    synthetic_orders().to_csv(path, index=False, compression="gzip")


def read_formal_tables(root: Path) -> dict[str, pd.DataFrame]:
    return {
        name: pd.read_csv(root / spec["filename"])
        for name, spec in TABLES.items()
    }


def assert_table_outputs_equal(left: Path, right: Path) -> None:
    for name, spec in TABLES.items():
        pd.testing.assert_frame_equal(
            pd.read_csv(left / spec["filename"]),
            pd.read_csv(right / spec["filename"]),
            check_dtype=True,
        )


def test_tiny_supply_outputs_are_formal_and_complete(tmp_path: Path) -> None:
    orders_path = tmp_path / "orders.csv.gz"
    write_orders(orders_path)
    output = tmp_path / "supply"
    summary = supply.run_pipeline(orders_path=orders_path, output_dir=output, slot_duration_min=10, n_blocks=10)

    assert summary["orders_loaded"] == 7
    assert set(path.name for path in output.iterdir()) - {"run.log"} == FORMAL_FILES
    assert not any((output / name).exists() for name in [
        "trip_segments.csv.gz", "driver_chains.csv.gz", "idle_windows.csv.gz",
        "run_summary.partial.json", "_SUCCESS",
    ])


def test_supply_contract_accepts_tiny_formal_outputs(tmp_path: Path) -> None:
    orders_path = tmp_path / "orders.csv.gz"
    write_orders(orders_path)
    output = tmp_path / "supply"
    supply.run_pipeline(orders_path=orders_path, output_dir=output, slot_duration_min=10, n_blocks=10)

    result = validate_supply_outputs(output, expected_cluster_ids=[10, 30, 50, 70], chunksize=1)

    assert result["run_summary"]["orders_loaded"] == 7
    assert result["available_floor"]["rows"] == result["fleet_lower_bound"]["rows"]
    assert result["inservice_od"]["totals"]["vehicles_in_service"] > 0
    fleet = result["fleet_lower_bound"]
    assert fleet["fleet_global_repeated_row_sum"] == (
        fleet["fleet_global_unique_time_sum"] * result["run_summary"]["global_clusters"]
    )


def test_supply_contract_rejects_inconsistent_repeated_global_fleet(tmp_path: Path) -> None:
    orders_path = tmp_path / "orders.csv.gz"
    write_orders(orders_path)
    output = tmp_path / "supply"
    supply.run_pipeline(orders_path=orders_path, output_dir=output, slot_duration_min=10, n_blocks=10)
    fleet_path = output / "supply_fleet_lower_bound.csv.gz"
    fleet = pd.read_csv(fleet_path)
    fleet.loc[1, "global_fleet_lower_bound"] += 1
    fleet.to_csv(fleet_path, index=False, compression="gzip")

    with pytest.raises(ValueError, match="global_fleet_lower_bound differs"):
        validate_supply_outputs(output, chunksize=1)


def test_partial_summary_never_satisfies_supply_contract(tmp_path: Path) -> None:
    (tmp_path / "run_summary.partial.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="partial summary"):
        validate_supply_outputs(tmp_path)


def test_block_count_and_execution_order_do_not_change_outputs(tmp_path: Path, monkeypatch) -> None:
    orders_path = tmp_path / "orders.csv.gz"
    write_orders(orders_path)
    one = tmp_path / "one"
    many = tmp_path / "many"
    reversed_dir = tmp_path / "reversed"
    supply.run_pipeline(orders_path=orders_path, output_dir=one, slot_duration_min=10, n_blocks=1)
    supply.run_pipeline(orders_path=orders_path, output_dir=many, slot_duration_min=10, n_blocks=10)
    original_range = builtins.range
    monkeypatch.setattr(supply, "range", lambda stop: reversed(original_range(stop)), raising=False)
    supply.run_pipeline(orders_path=orders_path, output_dir=reversed_dir, slot_duration_min=10, n_blocks=10)

    assert_table_outputs_equal(one, many)
    assert_table_outputs_equal(many, reversed_dir)


def test_driver_blocks_are_disjoint_complete_and_allow_empty_blocks() -> None:
    drivers = pd.Series([101, 101, 202, 303, 404, 505], dtype="int64")
    blocks = supply.driver_block_id(drivers, 20)
    mapping: dict[int, set[int]] = {}
    for driver, block in zip(drivers, blocks):
        mapping.setdefault(int(driver), set()).add(int(block))
    assert all(len(values) == 1 for values in mapping.values())
    assert set(mapping) == set(drivers)
    assert len(set(blocks)) < 20


def test_failed_output_write_has_no_success_summary(tmp_path: Path, monkeypatch) -> None:
    orders_path = tmp_path / "orders.csv.gz"
    write_orders(orders_path)
    output = tmp_path / "failed"
    original = supply.save_csv_gz
    calls = 0

    def fail_second(frame, path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic write failure")
        original(frame, path)

    monkeypatch.setattr(supply, "save_csv_gz", fail_second)
    with pytest.raises(OSError, match="synthetic write failure"):
        supply.run_pipeline(orders_path=orders_path, output_dir=output, slot_duration_min=10, n_blocks=3)
    assert not (output / "run_summary.json").exists()
    assert not (output / "run_summary.partial.json").exists()
    assert not (output / "_SUCCESS").exists()


def test_run_supply_uses_owned_stage_directory(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run_pipeline(**kwargs):
        calls.append(kwargs)
        output = Path(kwargs["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        for filename in FORMAL_FILES:
            (output / filename).write_text("{}" if filename.endswith(".json") else "x\n", encoding="utf-8")
        return {
            "orders_loaded": 7, "n_drivers": 5, "global_slots": 12,
            "global_clusters": 4, "in_service_rows": 9,
            "available_rows": 48, "fleet_rows": 48,
        }

    monkeypatch.setattr(supply, "run_pipeline", fake_run_pipeline)
    config_path = tmp_path / "config.yaml"
    config = ResolvedStageConfig(
        config_path,
        {"stage3_supply": {
            "orders_path": "inputs/orders.csv.gz", "output_dir": "escape",
            "max_gap_minutes": 60, "tau_idle_minutes": 30,
            "carpool_merge_gap_s": 0, "slot_duration_min": 10, "n_blocks": 8,
        }},
        "fingerprint",
    )
    context = RunContext("run", tmp_path / "run", tmp_path).for_stage("supply")

    result = supply.run_supply(config, context)

    assert result.status is StageStatus.COMPLETE
    assert calls[0]["output_dir"] == context.stage_dir
    assert calls[0]["orders_path"] == (tmp_path / "inputs/orders.csv.gz").resolve()
    assert all(path.parent == context.stage_dir for path in result.outputs.values())
    assert not (context.stage_dir / "_SUCCESS").exists()
