from __future__ import annotations

import gzip
from contextlib import contextmanager
import inspect
import io
from pathlib import Path
import struct
import zlib

import pandas as pd
import pytest
import yaml

from roadnet_partition.config import (
    ConfigError,
    resolve_demand_config,
    validate_gzip_compresslevel,
)
from roadnet_partition.downstream import demand
from roadnet_partition.pipeline import timing
from roadnet_partition.pipeline.timing import StageTimer, open_timed_gzip_text


CSV_BYTES = (
    b"stage_id,source_file,source_row,order_id,driver_id,departure_time,finish_time,"
    b"slot_start,pickup_seg_id,dropoff_seg_id,origin_cluster_id,destination_cluster_id,"
    b"pickup_match_distance_m,dropoff_match_distance_m,service_type\n"
    b"1,synthetic.csv,2,order-a,driver-a,2017-06-01 00:00:00,2017-06-01 00:10:00,"
    b"2017-06-01 00:00:00,seg-a,seg-b,1,2,1.0,2.0,exclusive\n"
    b"2,synthetic.csv,3,order-b,driver-b,2017-06-01 00:10:00,2017-06-01 00:20:00,"
    b"2017-06-01 00:10:00,seg-b,seg-c,2,3,1.5,2.5,carpool\n"
)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("level", [1, 3, 6, 9])
def test_supported_levels_are_passed_to_writer_and_preserve_csv_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    level: int,
) -> None:
    captured: list[int] = []
    original = zlib.compressobj

    def capture(*args, **kwargs):
        captured.append(args[0] if args else kwargs["level"])
        return original(*args, **kwargs)

    monkeypatch.setattr(zlib, "compressobj", capture)
    output = tmp_path / f"level-{level}-{enabled}.csv.gz"
    timer = StageTimer("test", enabled)
    with open_timed_gzip_text(output, timer, compresslevel=level) as (handle, _):
        handle.write(CSV_BYTES.decode("utf-8"))

    with gzip.open(output, "rb") as handle:
        assert handle.read() == CSV_BYTES
    assert captured == [level]
    with output.open("rb") as handle:
        header = handle.read(64)
    assert header[:2] == b"\x1f\x8b"
    assert header[3] & 8
    assert header[10:].split(b"\0", 1)[0].decode("ascii") == output.name.removesuffix(".gz")
    assert struct.unpack("<I", header[4:8])[0] > 0
    assert header[9] == 255


@pytest.mark.parametrize("value", [True, False, -1, 10, 1.5, "1", None])
def test_invalid_gzip_compresslevel_is_rejected(value) -> None:
    with pytest.raises(ConfigError, match=r"gzip_compresslevel.*\[0, 9\]"):
        validate_gzip_compresslevel(value)


def test_production_demand_config_has_an_explicit_integer_level() -> None:
    root = Path(__file__).resolve().parents[1]
    resolved = resolve_demand_config(root / "configs/pipelines/demand.yaml")
    value = resolved.values["gzip_compresslevel"]
    assert type(value) is int
    assert value == 1


def test_resolver_rejects_invalid_gzip_level_early(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    values = yaml.safe_load((root / "configs/pipelines/demand.yaml").read_text(encoding="utf-8"))
    values["dataset_config"] = str(root / "configs/datasets/fifth_ring.yaml")
    values["gzip_compresslevel"] = 10
    source = tmp_path / "demand-invalid.yaml"
    source.write_text(yaml.safe_dump(values), encoding="utf-8")

    with pytest.raises(ConfigError, match=r"gzip_compresslevel.*\[0, 9\]"):
        resolve_demand_config(source)


def test_export_keeps_assigned_batch_size_and_forwards_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[int] = []

    @contextmanager
    def fake_open(_path, _timer, compresslevel):
        captured.append(compresslevel)
        handle = io.StringIO()
        yield handle, None

    monkeypatch.setattr(timing, "open_timed_gzip_text", fake_open)
    monkeypatch.setattr(timing, "get_active_timer", lambda: StageTimer("test", False))
    monkeypatch.setattr(
        demand,
        "_iter_export_chunks",
        lambda *_args: iter([(1, pd.DataFrame({
            "stage_id": [1], "source_file": ["synthetic.csv"], "source_row": [1],
            "order_id": ["order-a"], "driver_id": ["driver-a"],
            "departure_time_ns": [1496275200000000000], "finish_time_ns": [1496275800000000000],
            "slot_start_ns": [1496275200000000000], "pickup_seg_id": ["a"], "dropoff_seg_id": ["b"],
            "origin_cluster_id": [1], "destination_cluster_id": [2],
            "pickup_match_distance_m": [1.0], "dropoff_match_distance_m": [2.0],
            "service_type": ["exclusive"],
        }))]),
    )

    demand.export_assigned_orders(object(), tmp_path / "assigned.csv.gz", compresslevel=3)

    assert captured == [3]
    assert inspect.signature(demand.export_assigned_orders).parameters["chunksize"].default == 100000
