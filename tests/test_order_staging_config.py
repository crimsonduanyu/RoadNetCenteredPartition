from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from roadnet_partition.config import (
    DEFAULT_ORDER_STAGING,
    ConfigError,
    resolve_demand_config,
    validate_order_staging,
)


def test_demand_defaults_to_sqlite_v1_and_pins_columnar_settings() -> None:
    root = Path(__file__).resolve().parents[1]
    resolved = resolve_demand_config(root / "configs/pipelines/demand.yaml")
    assert resolved.values["order_staging_backend"] == "sqlite_v1"
    assert resolved.values["order_staging"] == DEFAULT_ORDER_STAGING


def test_demand_resolves_explicit_parquet_backend_into_config_fingerprint(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    values = yaml.safe_load((root / "configs/pipelines/demand.yaml").read_text(encoding="utf-8"))
    values["dataset_config"] = str(root / "configs/datasets/fifth_ring.yaml")
    values["order_staging_backend"] = "parquet_duckdb_v2"
    values["order_staging"] = {
        "memory_limit": "1GB",
        "threads": 2,
        "batch_size": 32,
        "target_shard_rows": 100,
        "temp_disk_budget_bytes": 40 * 1024**3,
        "compatibility_export": True,
    }
    source = tmp_path / "demand-v2.yaml"
    source.write_text(yaml.safe_dump(values), encoding="utf-8")
    resolved = resolve_demand_config(source)
    assert resolved.values["order_staging_backend"] == "parquet_duckdb_v2"
    assert resolved.values["order_staging"]["threads"] == 2
    assert resolved.fingerprint


@pytest.mark.parametrize(
    ("backend", "settings"),
    [
        ("unknown", None),
        ("parquet_duckdb_v2", {"memory_limit": "not-a-size"}),
        ("parquet_duckdb_v2", {"threads": 0}),
        ("parquet_duckdb_v2", {"compatibility_export": 1}),
    ],
)
def test_invalid_order_staging_is_rejected(backend, settings) -> None:
    with pytest.raises(ConfigError):
        validate_order_staging(backend, settings)
