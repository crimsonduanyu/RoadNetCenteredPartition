from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterator, Mapping

import pandas as pd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from roadnet_partition.config import stable_value
from roadnet_partition.io.manifests import atomic_write_json, file_record


MATCHED_ORDER_CHECKPOINT = "MatchedOrderCheckpointV1"
LABELED_ORDER_CHECKPOINT = "LabeledOrderCheckpointV1"
CHECKPOINT_MANIFEST_FILENAME = "checkpoint_manifest.json"
CHECKPOINT_COMPLETE_FILENAME = "_CHECKPOINT_COMPLETE"
CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_BACKEND = "parquet_duckdb_v2"

MATCHED_COLUMNS = (
    "stage_id",
    "source_file",
    "source_row",
    "order_id",
    "driver_id",
    "departure_time_ns",
    "finish_time_ns",
    "slot_start_ns",
    "pickup_seg_id",
    "dropoff_seg_id",
    "origin_cluster_id",
    "destination_cluster_id",
    "pickup_match_distance_m",
    "dropoff_match_distance_m",
)
LABELED_COLUMNS = MATCHED_COLUMNS + ("service_type",)
SORT_KEY = ("driver_id", "departure_time_ns", "finish_time_ns", "stage_id")

_TYPE_NAMES = {
    "stage_id": pa.int64(),
    "source_file": pa.string(),
    "source_row": pa.int64(),
    "order_id": pa.string(),
    "driver_id": pa.string(),
    "departure_time_ns": pa.int64(),
    "finish_time_ns": pa.int64(),
    "slot_start_ns": pa.int64(),
    "pickup_seg_id": pa.string(),
    "dropoff_seg_id": pa.string(),
    "origin_cluster_id": pa.string(),
    "destination_cluster_id": pa.string(),
    "pickup_match_distance_m": pa.float64(),
    "dropoff_match_distance_m": pa.float64(),
    "service_type": pa.string(),
}

# The two match-distance and order-id fields mirror the nullable SQLite columns.
# All other fields are produced only after the corresponding validity checks.
NULLABLE_POLICY = {
    name: name in {"order_id", "pickup_match_distance_m", "dropoff_match_distance_m"}
    for name in LABELED_COLUMNS
}

_SETTING_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?\s*(?:B|KB|MB|GB|TB)$", re.IGNORECASE)


def checkpoint_columns(kind: str) -> tuple[str, ...]:
    if kind == MATCHED_ORDER_CHECKPOINT:
        return MATCHED_COLUMNS
    if kind == LABELED_ORDER_CHECKPOINT:
        return LABELED_COLUMNS
    raise ValueError(f"unknown checkpoint kind: {kind!r}")


def checkpoint_schema(kind: str) -> pa.Schema:
    return pa.schema([
        pa.field(name, _TYPE_NAMES[name], nullable=NULLABLE_POLICY[name])
        for name in checkpoint_columns(kind)
    ])


def schema_fingerprint(schema: pa.Schema) -> str:
    payload = [
        {"name": field.name, "type": str(field.type), "nullable": bool(field.nullable)}
        for field in schema
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def checkpoint_schema_fingerprint(kind: str) -> str:
    return schema_fingerprint(checkpoint_schema(kind))


def deterministic_shard_id(prefix: str, file_index: int, chunk_index: int) -> str:
    if not prefix or not re.fullmatch(r"[a-z][a-z0-9_-]*", prefix):
        raise ValueError(f"invalid shard prefix: {prefix!r}")
    if file_index < 0 or chunk_index < 0:
        raise ValueError("file and chunk indexes must be non-negative")
    return f"{prefix}-{file_index:06d}-{chunk_index:06d}"


def runtime_fingerprint(runtime: Mapping[str, Any]) -> str:
    payload = json.dumps(
        stable_value(runtime),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _value(value: Any) -> str | int | float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def _sort_key_bounds(frame: pd.DataFrame) -> tuple[list[Any] | None, list[Any] | None]:
    if frame.empty:
        return None, None
    ordered = frame.sort_values(list(SORT_KEY), kind="mergesort")
    first = ordered.iloc[0]
    last = ordered.iloc[-1]
    return (
        [_value(first[name]) for name in SORT_KEY],
        [_value(last[name]) for name in SORT_KEY],
    )


def _validate_frame(frame: pd.DataFrame, kind: str) -> None:
    columns = checkpoint_columns(kind)
    if tuple(frame.columns) != columns:
        raise ValueError(f"{kind} columns differ: expected {columns}, got {tuple(frame.columns)}")
    if frame.columns.duplicated().any():
        raise ValueError(f"{kind} contains duplicate columns")
    for name in columns:
        if not NULLABLE_POLICY[name] and bool(frame[name].isna().any()):
            raise ValueError(f"{kind}.{name} is non-nullable")
    for name in SORT_KEY:
        if bool(frame[name].isna().any()):
            raise ValueError(f"{kind}.{name} is part of the non-null sort key")


def _table_from_frame(frame: pd.DataFrame, kind: str) -> pa.Table:
    _validate_frame(frame, kind)
    return pa.Table.from_pandas(
        frame.loc[:, checkpoint_columns(kind)],
        schema=checkpoint_schema(kind),
        preserve_index=False,
        safe=True,
    )


def _directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _owned_relative(path: Path, root: Path) -> str:
    resolved = path.resolve()
    owner = root.resolve()
    if not resolved.is_relative_to(owner):
        raise ValueError(f"path escapes checkpoint root: {path}")
    return resolved.relative_to(owner).as_posix()


def _write_parquet_atomic(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.unlink(missing_ok=True)
    try:
        pq.write_table(
            table,
            temporary,
            compression="zstd",
            version="2.6",
            write_statistics=True,
        )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _manifest_template(
    *,
    kind: str,
    source_fingerprint: str,
    config_fingerprint: str,
    runtime: Mapping[str, Any],
    duckdb_version: str | None,
    stage_id_validation: str,
    source_manifest_fingerprint: str | None,
) -> dict[str, Any]:
    schema = checkpoint_schema(kind)
    columns = checkpoint_columns(kind)
    return {
        "contract": kind,
        "backend": CHECKPOINT_BACKEND,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_kind": kind,
        "columns": [
            {"name": name, "arrow_type": str(_TYPE_NAMES[name]), "nullable": NULLABLE_POLICY[name]}
            for name in columns
        ],
        "nullable_policy": dict(NULLABLE_POLICY),
        "row_count": 0,
        "global_ordinal_range": [0, 0],
        "sort_key": list(SORT_KEY),
        "stage_id_unique": True,
        "stage_id_validation": stage_id_validation,
        "stage_id_validation_source": (
            MATCHED_ORDER_CHECKPOINT if stage_id_validation == "inherited_source" else None
        ),
        "stage_id_validation_source_fingerprint": source_manifest_fingerprint,
        "min_sort_key": None,
        "max_sort_key": None,
        "min_stage_id": None,
        "max_stage_id": None,
        "shards": [],
        "source_fingerprint": source_fingerprint,
        "config_fingerprint": config_fingerprint,
        "runtime": stable_value(runtime),
        "runtime_fingerprint": runtime_fingerprint(runtime),
        "duckdb_version": duckdb_version,
        "arrow_schema_fingerprint": schema_fingerprint(schema),
        "parquet_schema_fingerprint": schema_fingerprint(schema),
        "completed_marker": CHECKPOINT_COMPLETE_FILENAME,
        "completed": False,
        "atomic_publish": "staging",
        "status": "running",
    }


def validate_checkpoint_manifest(
    manifest: Any,
    *,
    root: Path | None = None,
    expected_kind: str | None = None,
    require_complete: bool = True,
) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("checkpoint manifest must be an object")
    required = {
        "contract", "backend", "schema_version", "checkpoint_kind", "columns", "nullable_policy", "row_count",
        "global_ordinal_range", "sort_key", "stage_id_unique", "shards", "source_fingerprint",
        "stage_id_validation", "stage_id_validation_source", "stage_id_validation_source_fingerprint",
        "config_fingerprint", "runtime", "runtime_fingerprint", "duckdb_version",
        "arrow_schema_fingerprint", "parquet_schema_fingerprint", "completed_marker", "completed",
        "atomic_publish", "status",
    }
    missing = required - set(manifest)
    if missing:
        raise ValueError(f"checkpoint manifest missing fields: {sorted(missing)}")
    kind = manifest["checkpoint_kind"]
    if kind not in {MATCHED_ORDER_CHECKPOINT, LABELED_ORDER_CHECKPOINT}:
        raise ValueError(f"unsupported checkpoint kind: {kind!r}")
    if expected_kind is not None and kind != expected_kind:
        raise ValueError(f"checkpoint kind differs: {kind!r} != {expected_kind!r}")
    if manifest["contract"] != kind or manifest["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("checkpoint contract/version differs")
    if manifest["backend"] != CHECKPOINT_BACKEND:
        raise ValueError("checkpoint backend differs")
    if tuple(item.get("name") for item in manifest["columns"]) != checkpoint_columns(kind):
        raise ValueError("checkpoint columns differ")
    expected_schema_fp = checkpoint_schema_fingerprint(kind)
    if manifest["arrow_schema_fingerprint"] != expected_schema_fp:
        raise ValueError("Arrow schema fingerprint differs")
    if manifest["parquet_schema_fingerprint"] != expected_schema_fp:
        raise ValueError("Parquet schema fingerprint differs")
    if manifest["nullable_policy"] != NULLABLE_POLICY:
        raise ValueError("nullable policy differs")
    if tuple(manifest["sort_key"]) != SORT_KEY or manifest["stage_id_unique"] is not True:
        raise ValueError("checkpoint sort or uniqueness contract differs")
    stage_id_validation = manifest["stage_id_validation"]
    if stage_id_validation not in {"unchecked", "sequential", "inherited_source"}:
        raise ValueError("checkpoint stage_id validation mode differs")
    if stage_id_validation == "inherited_source" and manifest["stage_id_validation_source"] != MATCHED_ORDER_CHECKPOINT:
        raise ValueError("checkpoint inherited stage_id source differs")
    source_manifest_fingerprint = manifest["stage_id_validation_source_fingerprint"]
    if stage_id_validation == "inherited_source":
        if not isinstance(source_manifest_fingerprint, str) or not source_manifest_fingerprint:
            raise ValueError("checkpoint inherited stage_id source fingerprint is missing")
    elif source_manifest_fingerprint is not None:
        raise ValueError("checkpoint sequential stage_id source fingerprint is unexpected")
    if not isinstance(manifest["shards"], list) or int(manifest["row_count"]) < 0:
        raise ValueError("checkpoint row/shard metadata is invalid")
    if manifest["completed"]:
        if manifest["status"] != "complete" or manifest["atomic_publish"] != "complete":
            raise ValueError("completed checkpoint has incomplete publish state")
        if root is not None and not (root / str(manifest["completed_marker"])).is_file():
            raise ValueError("completed checkpoint marker is missing")
    elif require_complete:
        raise ValueError("checkpoint is not complete")
    previous_end = 0
    stage_ids: set[int] = set()
    for shard in manifest["shards"]:
        for field in (
            "shard_id", "path", "row_count", "global_ordinal_start", "global_ordinal_end",
            "min_sort_key", "max_sort_key", "min_stage_id", "max_stage_id", "file_size", "sha256",
            "schema_fingerprint", "completed",
        ):
            if field not in shard:
                raise ValueError(f"checkpoint shard missing {field!r}")
        if shard["completed"] is not True:
            raise ValueError("checkpoint contains an incomplete shard")
        if int(shard["global_ordinal_start"]) != previous_end:
            raise ValueError("checkpoint ordinal ranges are not contiguous")
        if int(shard["global_ordinal_end"]) - int(shard["global_ordinal_start"]) != int(shard["row_count"]):
            raise ValueError("checkpoint shard ordinal range differs from row count")
        previous_end = int(shard["global_ordinal_end"])
        if shard["schema_fingerprint"] != expected_schema_fp:
            raise ValueError("checkpoint shard schema fingerprint differs")
        if root is not None:
            path = (root / str(shard["path"])).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError(f"checkpoint shard is missing or escapes root: {path}")
            record = file_record(path)
            if record["size"] != int(shard["file_size"]) or record["sha256"] != shard["sha256"]:
                raise ValueError(f"checkpoint shard hash/size differs: {path}")
            if schema_fingerprint(pq.read_schema(path)) != expected_schema_fp:
                raise ValueError(f"checkpoint Parquet schema differs: {path}")
            if stage_id_validation == "unchecked" and int(shard["row_count"]):
                stage_ids.update(
                    int(value)
                    for value in pq.read_table(path, columns=["stage_id"])["stage_id"].to_pylist()
                )
    if previous_end != int(manifest["row_count"]):
        raise ValueError("checkpoint row count differs from shard ranges")
    if int(manifest["global_ordinal_range"][0]) != 0 or int(manifest["global_ordinal_range"][1]) != previous_end:
        raise ValueError("checkpoint global ordinal range differs")
    if manifest["stage_id_unique"]:
        if stage_id_validation == "sequential":
            if int(manifest["row_count"]) and (
                int(manifest["min_stage_id"]) != 1
                or int(manifest["max_stage_id"]) != int(manifest["row_count"])
            ):
                raise ValueError("sequential checkpoint stage_id range differs")
        elif stage_id_validation == "unchecked" and len(stage_ids) != int(manifest["row_count"]):
            raise ValueError("checkpoint stage_id values are not unique")


class ParquetCheckpointWriter:
    def __init__(
        self,
        root: Path,
        *,
        kind: str,
        source_fingerprint: str,
        config_fingerprint: str,
        runtime: Mapping[str, Any],
        duckdb_version: str | None,
        target_rows: int = 500_000,
        stage_id_validation: str = "unchecked",
        source_manifest_fingerprint: str | None = None,
    ) -> None:
        if target_rows <= 0:
            raise ValueError("target_rows must be positive")
        self.root = Path(root).resolve()
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError(f"checkpoint root is not empty: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        self.kind = kind
        self.target_rows = int(target_rows)
        self._manifest_path = self.root / CHECKPOINT_MANIFEST_FILENAME
        self._manifest = _manifest_template(
            kind=kind,
            source_fingerprint=source_fingerprint,
            config_fingerprint=config_fingerprint,
            runtime=runtime,
            duckdb_version=duckdb_version,
            stage_id_validation=stage_id_validation,
            source_manifest_fingerprint=source_manifest_fingerprint,
        )
        atomic_write_json(self._manifest_path, self._manifest)
        self._row_count = 0
        self._closed = False

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    @property
    def row_count(self) -> int:
        return self._row_count

    def _write_manifest(self) -> None:
        atomic_write_json(self._manifest_path, self._manifest)

    def write_frame(self, frame: pd.DataFrame, shard_id: str) -> None:
        if self._closed:
            raise RuntimeError("checkpoint writer is closed")
        if any(shard["shard_id"] == shard_id for shard in self._manifest["shards"]):
            raise ValueError(f"duplicate checkpoint shard ID: {shard_id}")
        if self._manifest["stage_id_validation"] == "sequential" and not frame.empty:
            actual = frame["stage_id"].to_numpy(dtype=np.int64)
            expected = np.arange(self._row_count + 1, self._row_count + len(frame) + 1, dtype=np.int64)
            if not np.array_equal(actual, expected):
                raise ValueError("sequential checkpoint stage_id values differ")
        table = _table_from_frame(frame, self.kind)
        path = self.root / "shards" / f"{shard_id}.parquet"
        _write_parquet_atomic(path, table)
        parquet_schema = pq.read_schema(path)
        expected_fp = checkpoint_schema_fingerprint(self.kind)
        if schema_fingerprint(parquet_schema) != expected_fp:
            path.unlink(missing_ok=True)
            raise ValueError(f"written Parquet schema differs for {path}")
        first_key, last_key = _sort_key_bounds(frame)
        row_count = int(len(frame))
        shard = {
            "shard_id": shard_id,
            "path": _owned_relative(path, self.root),
            "row_count": row_count,
            "global_ordinal_start": self._row_count,
            "global_ordinal_end": self._row_count + row_count,
            "min_sort_key": first_key,
            "max_sort_key": last_key,
            "min_stage_id": None if frame.empty else int(frame["stage_id"].min()),
            "max_stage_id": None if frame.empty else int(frame["stage_id"].max()),
            "file_size": path.stat().st_size,
            "sha256": file_record(path)["sha256"],
            "schema_fingerprint": schema_fingerprint(parquet_schema),
            "completed": True,
        }
        self._manifest["shards"].append(shard)
        self._row_count += row_count
        self._manifest["row_count"] = self._row_count
        self._manifest["global_ordinal_range"] = [0, self._row_count]
        if first_key is not None:
            if self._manifest["min_sort_key"] is None or tuple(first_key) < tuple(self._manifest["min_sort_key"]):
                self._manifest["min_sort_key"] = first_key
            if self._manifest["max_sort_key"] is None or tuple(last_key) > tuple(self._manifest["max_sort_key"]):
                self._manifest["max_sort_key"] = last_key
            min_stage_id = int(frame["stage_id"].min())
            max_stage_id = int(frame["stage_id"].max())
            current_min = self._manifest["min_stage_id"]
            current_max = self._manifest["max_stage_id"]
            self._manifest["min_stage_id"] = min_stage_id if current_min is None else min(current_min, min_stage_id)
            self._manifest["max_stage_id"] = max_stage_id if current_max is None else max(current_max, max_stage_id)
        self._write_manifest()

    def finish(self) -> Path:
        if self._closed:
            raise RuntimeError("checkpoint writer is already closed")
        self._manifest["status"] = "finalizing"
        self._manifest["atomic_publish"] = "finalizing"
        self._write_manifest()
        atomic_write_json(
            self.root / CHECKPOINT_COMPLETE_FILENAME,
            {
                "contract": self.kind,
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "row_count": self._row_count,
                "schema_fingerprint": self._manifest["arrow_schema_fingerprint"],
            },
        )
        self._manifest["status"] = "complete"
        self._manifest["atomic_publish"] = "complete"
        self._manifest["completed"] = True
        self._write_manifest()
        validate_checkpoint_manifest(self._manifest, root=self.root, expected_kind=self.kind)
        self._closed = True
        return self._manifest_path


class DriverBoundaryCheckpointWriter(ParquetCheckpointWriter):
    """Write labeled shards without closing a durable shard inside a driver."""

    def __init__(self, *args: Any, target_rows: int = 500_000, **kwargs: Any) -> None:
        kwargs.setdefault("stage_id_validation", "inherited_source")
        super().__init__(*args, target_rows=target_rows, **kwargs)
        self._pending: list[pd.DataFrame] = []
        self._pending_rows = 0
        self._shard_number = 0

    def write_driver(self, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        if self._pending and self._pending_rows + len(frame) > self.target_rows:
            self._flush_pending()
        self._pending.append(frame)
        self._pending_rows += len(frame)
        if self._pending_rows >= self.target_rows:
            self._flush_pending()

    def _flush_pending(self) -> None:
        if not self._pending:
            return
        frame = self._pending[0] if len(self._pending) == 1 else pd.concat(self._pending, ignore_index=True)
        shard_id = f"labeled-{self._shard_number:06d}"
        self.write_frame(frame.reset_index(drop=True), shard_id)
        self._shard_number += 1
        self._pending = []
        self._pending_rows = 0

    def finish(self) -> Path:
        self._flush_pending()
        return super().finish()


def load_checkpoint_manifest(
    path: str | Path,
    *,
    expected_kind: str | None = None,
    expected_source_fingerprint: str | None = None,
    expected_config_fingerprint: str | None = None,
    expected_runtime_fingerprint: str | None = None,
    expected_duckdb_version: str | None = None,
) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_checkpoint_manifest(manifest, root=manifest_path.parent, expected_kind=expected_kind)
    expected = {
        "source_fingerprint": expected_source_fingerprint,
        "config_fingerprint": expected_config_fingerprint,
        "runtime_fingerprint": expected_runtime_fingerprint,
        "duckdb_version": expected_duckdb_version,
    }
    for field, value in expected.items():
        if value is not None and manifest.get(field) != value:
            raise ValueError(f"checkpoint {field} differs")
    return manifest


def iter_checkpoint_batches(
    manifest_path: str | Path,
    *,
    columns: list[str] | tuple[str, ...] | None = None,
    batch_size: int = 100_000,
) -> Iterator[pa.RecordBatch]:
    manifest = load_checkpoint_manifest(manifest_path)
    root = Path(manifest_path).resolve().parent
    selected = list(columns or checkpoint_columns(manifest["checkpoint_kind"]))
    unknown = set(selected) - set(checkpoint_columns(manifest["checkpoint_kind"]))
    if unknown:
        raise ValueError(f"unknown checkpoint columns: {sorted(unknown)}")
    for shard in manifest["shards"]:
        parquet = pq.ParquetFile(root / shard["path"])
        yield from parquet.iter_batches(batch_size=batch_size, columns=selected, use_threads=False)


def _validate_setting(value: str) -> str:
    if not isinstance(value, str) or not _SETTING_RE.fullmatch(value.strip()):
        raise ValueError("DuckDB memory_limit must be a size such as '512MB'")
    return value.strip().upper().replace(" ", "")


def _temp_dir_peak(path: Path, peak: int) -> int:
    return max(peak, _directory_size(path))


@contextmanager
def _sorted_checkpoint_batches(
    manifest_path: str | Path,
    *,
    expected_kind: str,
    order_key: tuple[str, ...],
    temp_directory: Path,
    run_owned_root: Path,
    memory_limit: str = "512MB",
    threads: int = 1,
    batch_size: int = 100_000,
) -> Iterator[tuple[Iterator[pa.RecordBatch], dict[str, Any]]]:
    """Yield one deterministic DuckDB external-sort Arrow stream."""
    if threads <= 0 or batch_size <= 0:
        raise ValueError("DuckDB threads and Arrow batch_size must be positive")
    memory_limit = _validate_setting(memory_limit)
    temp_directory = Path(temp_directory).resolve()
    run_owned_root = Path(run_owned_root).resolve()
    if not temp_directory.is_relative_to(run_owned_root):
        raise ValueError(f"DuckDB temp directory is not run-owned: {temp_directory}")
    temp_directory.mkdir(parents=True, exist_ok=True)
    manifest = load_checkpoint_manifest(manifest_path, expected_kind=expected_kind)
    root = Path(manifest_path).resolve().parent
    paths = [str((root / shard["path"]).resolve()) for shard in manifest["shards"]]
    import duckdb

    if not paths:
        empty_metrics = {
            "duckdb_version": duckdb.__version__,
            "memory_limit": memory_limit,
            "threads": int(threads),
            "temp_directory": temp_directory.as_posix(),
            "sort_key": list(order_key),
            "sort_wall_seconds": 0.0,
            "temp_spill_bytes": _directory_size(temp_directory),
            "temp_peak_disk_bytes": _directory_size(temp_directory),
        }
        yield iter(()), empty_metrics
        return

    connection = duckdb.connect(database=":memory:")
    peak_temp = _directory_size(temp_directory)
    started = time.perf_counter()
    try:
        connection.execute(f"SET memory_limit = '{memory_limit}'")
        connection.execute(f"SET threads = {int(threads)}")
        connection.execute("SET temp_directory = ?", [str(temp_directory)])
        select_columns = ", ".join(f'"{name}"' for name in checkpoint_columns(expected_kind))
        relation = connection.from_query(
            f"SELECT {select_columns} FROM read_parquet(?) "
            "ORDER BY " + ", ".join(order_key),
            params=[paths],
        )
        reader = relation.to_arrow_reader(batch_size=batch_size)

        def batches() -> Iterator[pa.RecordBatch]:
            nonlocal peak_temp
            for batch in reader:
                peak_temp = _temp_dir_peak(temp_directory, peak_temp)
                yield batch
            peak_temp = _temp_dir_peak(temp_directory, peak_temp)

        metrics = {
            "duckdb_version": duckdb.__version__,
            "memory_limit": memory_limit,
            "threads": int(threads),
            "temp_directory": temp_directory.as_posix(),
            "sort_key": list(order_key),
            "sort_wall_seconds": None,
            "temp_spill_bytes": None,
            "temp_peak_disk_bytes": None,
        }
        try:
            yield batches(), metrics
        finally:
            metrics["sort_wall_seconds"] = time.perf_counter() - started
            peak_temp = _temp_dir_peak(temp_directory, peak_temp)
            metrics["temp_spill_bytes"] = peak_temp
            metrics["temp_peak_disk_bytes"] = peak_temp
    finally:
        connection.close()


@contextmanager
def sorted_checkpoint_batches(
    matched_manifest: str | Path,
    *,
    temp_directory: Path,
    run_owned_root: Path,
    memory_limit: str = "512MB",
    threads: int = 1,
    batch_size: int = 100_000,
) -> Iterator[tuple[Iterator[pa.RecordBatch], dict[str, Any]]]:
    with _sorted_checkpoint_batches(
        matched_manifest,
        expected_kind=MATCHED_ORDER_CHECKPOINT,
        order_key=SORT_KEY,
        temp_directory=temp_directory,
        run_owned_root=run_owned_root,
        memory_limit=memory_limit,
        threads=threads,
        batch_size=batch_size,
    ) as result:
        yield result


@contextmanager
def sorted_labeled_checkpoint_batches(
    labeled_manifest: str | Path,
    *,
    temp_directory: Path,
    run_owned_root: Path,
    memory_limit: str = "512MB",
    threads: int = 1,
    batch_size: int = 100_000,
) -> Iterator[tuple[Iterator[pa.RecordBatch], dict[str, Any]]]:
    with _sorted_checkpoint_batches(
        labeled_manifest,
        expected_kind=LABELED_ORDER_CHECKPOINT,
        order_key=("stage_id",),
        temp_directory=temp_directory,
        run_owned_root=run_owned_root,
        memory_limit=memory_limit,
        threads=threads,
        batch_size=batch_size,
    ) as result:
        yield result


__all__ = [
    "CHECKPOINT_COMPLETE_FILENAME",
    "CHECKPOINT_BACKEND",
    "CHECKPOINT_MANIFEST_FILENAME",
    "CHECKPOINT_SCHEMA_VERSION",
    "DriverBoundaryCheckpointWriter",
    "LABELED_COLUMNS",
    "LABELED_ORDER_CHECKPOINT",
    "MATCHED_COLUMNS",
    "MATCHED_ORDER_CHECKPOINT",
    "NULLABLE_POLICY",
    "ParquetCheckpointWriter",
    "SORT_KEY",
    "checkpoint_columns",
    "checkpoint_schema",
    "checkpoint_schema_fingerprint",
    "deterministic_shard_id",
    "iter_checkpoint_batches",
    "load_checkpoint_manifest",
    "runtime_fingerprint",
    "schema_fingerprint",
    "sorted_checkpoint_batches",
    "sorted_labeled_checkpoint_batches",
    "validate_checkpoint_manifest",
]
