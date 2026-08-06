from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from roadnet_partition.downstream.demand import export_assigned_orders
from roadnet_partition.downstream.order_checkpoints import (
    LABELED_ORDER_CHECKPOINT,
    checkpoint_schema,
)
from roadnet_partition.downstream import supply
from roadnet_partition.pipeline.stages import PIPELINE_BINDINGS


BASE = {
    "departure_time": "2017-06-01 08:00:00",
    "finish_time": "2017-06-01 08:10:00",
    "origin_cluster_id": 1,
    "destination_cluster_id": 2,
    "service_type": "exclusive",
}


def write_supply_orders(path: Path, rows: list[dict]) -> None:
    pd.DataFrame([{**BASE, **row} for row in rows]).to_csv(path, index=False, compression="gzip")


def write_demand_artifact(path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE staged_orders (
          stage_id INTEGER PRIMARY KEY, source_file TEXT, source_row INTEGER,
          order_id TEXT, driver_id TEXT NOT NULL, departure_time_ns INTEGER,
          finish_time_ns INTEGER, slot_start_ns INTEGER, pickup_seg_id TEXT,
          dropoff_seg_id TEXT, origin_cluster_id TEXT, destination_cluster_id TEXT,
          pickup_match_distance_m REAL, dropoff_match_distance_m REAL
        );
        CREATE TABLE service_labels (stage_id INTEGER PRIMARY KEY, service_type TEXT NOT NULL);
        """
    )
    start = pd.Timestamp("2017-06-01 08:00:00").value
    identifiers = [
        ("o-1", "driver-A", "exclusive"),
        ("000123", "00042", "carpool"),
        (None, "driver-A", "carpool"),
        ("订单一", "司机甲", "exclusive"),
    ]
    for index, (order_id, driver_id, service) in enumerate(identifiers, 1):
        departure = start + index * 20 * 60 * 1_000_000_000
        finish = departure + 10 * 60 * 1_000_000_000
        connection.execute(
            "INSERT INTO staged_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (index, "tiny.csv", index - 1, order_id, driver_id, departure, finish,
             departure, "s1", "s2", "1", "2", 0.0, 0.0),
        )
        connection.execute("INSERT INTO service_labels VALUES (?, ?)", (index, service))
    export_assigned_orders(connection, path)


def test_authoritative_checkpoint_identifier_schema() -> None:
    schema = checkpoint_schema(LABELED_ORDER_CHECKPOINT)
    assert str(schema.field("order_id").type) == "string"
    assert schema.field("order_id").nullable
    assert str(schema.field("driver_id").type) == "string"
    assert not schema.field("driver_id").nullable


def test_identifier_tokens_and_unicode_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "orders.csv.gz"
    order_ids = ["o-1", "000123", "订单一", None, "NA", "N/A", "null", "None"]
    driver_ids = ["driver-A", "00042", "司机甲", "repeat", "NA", "N/A", "null", "None"]
    write_supply_orders(path, [
        {"order_id": order_id, "driver_id": driver_id}
        for order_id, driver_id in zip(order_ids, driver_ids)
    ])

    loaded = supply.load_orders(path)

    assert loaded["order_id"].tolist() == ["o-1", "000123", "订单一", pd.NA, "NA", "N/A", "null", "None"]
    assert loaded["driver_id"].tolist() == driver_ids
    assert str(loaded["order_id"].dtype) == "string"
    assert str(loaded["driver_id"].dtype) == "string"


@pytest.mark.parametrize("driver_id", [None, "", "   "])
def test_invalid_driver_fails_before_supply_output(tmp_path: Path, driver_id: str | None) -> None:
    path = tmp_path / "orders.csv.gz"
    output = tmp_path / "supply"
    marker = tmp_path / "marker"
    marker.write_bytes(b"unchanged")
    write_supply_orders(path, [{"order_id": "o-1", "driver_id": driver_id}])

    with pytest.raises(ValueError, match="driver_id.*CSV row"):
        supply.run_pipeline(orders_path=path, output_dir=output)

    assert not output.exists()
    assert marker.read_bytes() == b"unchanged"


def test_missing_column_and_invalid_service_fail_before_output(tmp_path: Path) -> None:
    missing = tmp_path / "missing.csv.gz"
    pd.DataFrame([{key: value for key, value in {**BASE, "order_id": "o", "driver_id": "d"}.items()
                  if key != "driver_id"}]).to_csv(missing, index=False, compression="gzip")
    with pytest.raises(ValueError, match="missing required columns.*driver_id"):
        supply.run_pipeline(orders_path=missing, output_dir=tmp_path / "missing-output")
    assert not (tmp_path / "missing-output").exists()

    invalid = tmp_path / "invalid.csv.gz"
    write_supply_orders(invalid, [{"order_id": "o", "driver_id": "d", "service_type": "unknown"}])
    with pytest.raises(ValueError, match="service_type.*CSV row"):
        supply.run_pipeline(orders_path=invalid, output_dir=tmp_path / "invalid-output")
    assert not (tmp_path / "invalid-output").exists()


def test_invalid_timestamps_are_left_for_established_filter(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "orders.csv.gz"
    write_supply_orders(path, [
        {"order_id": "bad", "driver_id": "d", "departure_time": "not-a-time"},
        {"order_id": "good", "driver_id": "d"},
    ])

    loaded = supply.load_orders(path)
    filtered = supply.filter_valid_orders(loaded)

    assert loaded["departure_time"].isna().tolist() == [True, False]
    assert filtered["order_id"].tolist() == ["good"]
    assert "Skipping 1 trips" in caplog.text


def test_demand_export_binds_to_real_supply_adapter_and_completes(tmp_path: Path) -> None:
    artifact = tmp_path / "orders_region_assigned.csv.gz"
    output = tmp_path / "supply"
    write_demand_artifact(artifact)

    loaded = supply.load_orders(artifact)
    assert loaded["order_id"].tolist() == ["o-1", "000123", pd.NA, "订单一"]
    assert loaded["driver_id"].tolist() == ["driver-A", "00042", "driver-A", "司机甲"]
    assert ("demand", "orders_region_assigned", "stage3_supply.orders_path", "assigned_orders") in PIPELINE_BINDINGS["supply"]

    summary = supply.run_pipeline(orders_path=artifact, output_dir=output, n_blocks=3)
    assert summary["orders_loaded"] == 4
    assert summary["n_drivers"] == 3
    assert (output / "supply_inservice_od.csv.gz").is_file()


def test_driver_identity_and_block_mapping_are_string_stable() -> None:
    drivers = pd.Series(["001", "1", "司机甲", "001"], dtype="string")
    first = supply.driver_block_id(drivers, 97)
    second = supply.driver_block_id(drivers, 97)
    assert np.array_equal(first, second)
    assert first[0] == first[3]
    assert first[0] != first[1]


def test_driver_block_mapping_ignores_python_hash_seed() -> None:
    code = (
        "from roadnet_partition.downstream.supply import driver_block_id; "
        "print(driver_block_id(['001','1','司机甲'], 97).tolist())"
    )
    outputs = []
    for seed in ("1", "999"):
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        outputs.append(subprocess.check_output([sys.executable, "-c", code], env=environment, text=True).strip())
    assert outputs[0] == outputs[1]


def test_nullable_order_ties_are_null_last_and_input_stable() -> None:
    frame = pd.DataFrame([
        {**BASE, "order_id": None, "driver_id": "d"},
        {**BASE, "order_id": "b", "driver_id": "d"},
        {**BASE, "order_id": None, "driver_id": "d"},
        {**BASE, "order_id": "a", "driver_id": "d"},
    ])
    frame["order_id"] = frame["order_id"].astype("string")
    frame["driver_id"] = frame["driver_id"].astype("string")
    frame["departure_time"] = pd.to_datetime(frame["departure_time"])
    frame["finish_time"] = pd.to_datetime(frame["finish_time"])

    groups = supply.resolve_carpool_trip_groups(frame.assign(service_type="carpool"))

    order_ids = groups.iloc[0]["order_ids"]
    assert order_ids[:2] == ["a", "b"]
    assert pd.isna(order_ids[2:]).all()
