from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
from typing import Any, Mapping

import yaml

from roadnet_partition.io.paths import resolve_path


@dataclass(frozen=True)
class ResolvedStageConfig:
    source_path: Path
    values: Mapping[str, Any]
    fingerprint: str
    stage: str | None = None
    scope: str | None = None
    project_root: Path | None = None
    dataset_path: Path | None = None


class ConfigError(ValueError):
    """A split configuration is invalid or internally inconsistent."""


DEFAULT_GZIP_COMPRESSLEVEL = 9
DEFAULT_ORDER_STAGING_BACKEND = "sqlite_v1"
ORDER_STAGING_BACKENDS = {"sqlite_v1", "parquet_duckdb_v2"}
DEFAULT_ORDER_STAGING = {
    "memory_limit": "512MB",
    "threads": 1,
    "batch_size": 100_000,
    "target_shard_rows": 500_000,
    "temp_disk_budget_bytes": 40 * 1024**3,
    "compatibility_export": True,
}


def validate_gzip_compresslevel(value: Any, *, source: Path | None = None) -> int:
    prefix = f"{source}: " if source is not None else ""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9:
        raise ConfigError(f"{prefix}gzip_compresslevel must be an integer in [0, 9]")
    return value


def validate_order_staging(
    backend: Any,
    settings: Mapping[str, Any] | None = None,
    *,
    source: Path | None = None,
) -> tuple[str, dict[str, Any]]:
    prefix = f"{source}: " if source is not None else ""
    backend = DEFAULT_ORDER_STAGING_BACKEND if backend is None else str(backend)
    if backend not in ORDER_STAGING_BACKENDS:
        raise ConfigError(f"{prefix}order_staging_backend must be one of {sorted(ORDER_STAGING_BACKENDS)}")
    values = dict(DEFAULT_ORDER_STAGING)
    if settings is not None:
        values.update(settings)
    memory_limit = values["memory_limit"]
    if not isinstance(memory_limit, str) or not _DUCKDB_MEMORY_LIMIT.fullmatch(memory_limit.strip()):
        raise ConfigError(f"{prefix}order_staging.memory_limit must be a size such as '512MB'")
    values["memory_limit"] = memory_limit.strip().upper().replace(" ", "")
    for name in ("threads", "batch_size", "target_shard_rows", "temp_disk_budget_bytes"):
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"{prefix}order_staging.{name} must be a positive integer")
        values[name] = int(value)
    if not isinstance(values["compatibility_export"], bool):
        raise ConfigError(f"{prefix}order_staging.compatibility_export must be boolean")
    return backend, values


_ANY = object()
_OPEN_MAPPING = object()
_OUTPUT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TTE_FILENAMES = {
    ("outputs", "count_filename"): "TTE_count.parquet",
    ("outputs", "hops_filename"): "TTE_hops.parquet",
    ("outputs", "support_filename"): "TTE_support.parquet",
    ("distance", "matrix_filename"): "cluster_network_distance.parquet",
    ("distance", "representatives_filename"): "cluster_representative_nodes.csv",
}
_DUCKDB_MEMORY_LIMIT = re.compile(r"^[0-9]+(?:\.[0-9]+)?\s*(?:B|KB|MB|GB|TB)$", re.IGNORECASE)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: yaml.SafeLoader, node: Any, deep: bool = False) -> dict[Any, Any]:
    mapping = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "YAML merge keys are not supported",
                key_node.start_mark,
            )
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


_DATASET_SCHEMA = {
    "schema_version": _ANY,
    "project_root": _ANY,
    "scope": _ANY,
    "crs": {"projected": _ANY, "geographic": _ANY},
    "study_area": {
        "label": _ANY,
        "center_point": {"lon": _ANY, "lat": _ANY},
        "harvest_bbox": {"north": _ANY, "south": _ANY, "east": _ANY, "west": _ANY},
        "ring_name_patterns": [_ANY],
        "exclude_name_patterns": [_ANY],
        "retain_boundary_roads": _ANY,
        "min_ring_segment_count": _ANY,
        "min_ring_total_length_km": _ANY,
        "min_ring_span_km": _ANY,
        "min_boundary_area_km2": _ANY,
        "max_boundary_area_km2": _ANY,
        "boundary_buffer_m": _ANY,
        "inside_length_ratio_threshold": _ANY,
        "boundary_tolerance_m": _ANY,
        "ring_overlap_tolerance_m": _ANY,
    },
    "paths": {
        "raw_root": _ANY,
        "interim_root": _ANY,
        "processed_root": _ANY,
        "raw_edges": _ANY,
        "raw_nodes": _ANY,
        "graphml": _ANY,
        "boundary": _ANY,
        "ring_segments": _ANY,
        "classified_edges": _ANY,
        "segment_nodes": _ANY,
    },
    "metadata": _OPEN_MAPPING,
    "notes": _ANY,
}

_PARTITION_SCHEMA = {
    "schema_version": _ANY,
    "dataset_config": _ANY,
    "scope": _ANY,
    "stage1_partition": {
        "graph_variant": _ANY,
        "regularized": {
            "initialization": _ANY,
            "inputs": {
                "graph": _ANY,
                "relation_edges": _ANY,
                "classified_edges": _ANY,
                "boundary": _ANY,
                "segment_nodes": _ANY,
                "poi_features": _ANY,
                "order_features": _ANY,
                "hourly_od": _ANY,
            },
            "baseline_clusters": {"*": _ANY},
            "objective": {
                "target_clusters": _ANY,
                "capacity_loss": _ANY,
                "capacity_min_ratio": _ANY,
                "capacity_max_ratio": _ANY,
                "lambda_g": _ANY,
                "lambda_r": _ANY,
                "alpha_cont": _ANY,
                "alpha_conn": _ANY,
                "grid": {
                    "lambda_c": [_ANY],
                    "lambda_r": [_ANY],
                    "alpha_cont": [_ANY],
                    "alpha_conn": [_ANY],
                },
            },
            "search": {
                "max_passes": _ANY,
                "min_delta": _ANY,
                "move_policy": _ANY,
                "enforce_connectivity": _ANY,
                "allow_merge_split": _ANY,
                "max_merge_candidates": _ANY,
                "max_merge_targets_per_cluster": _ANY,
                "max_split_candidates": _ANY,
                "split_cleanup_passes": _ANY,
                "grid": {"merge_split_enabled": [_ANY]},
            },
        },
        "outputs": {"run_root": _ANY, "canonical_partition": _ANY},
    },
    "standalone": {"output_dir": _ANY},
    "contract": {
        "verify_canonical": _ANY,
        "expected_partition": _ANY,
    },
    "metadata": _OPEN_MAPPING,
    "notes": _ANY,
}

_DEMAND_SCHEMA = {
    "schema_version": _ANY,
    "dataset_config": _ANY,
    "scope": _ANY,
    "gzip_compresslevel": _ANY,
    "order_staging_backend": _ANY,
    "order_staging": {
        "memory_limit": _ANY,
        "threads": _ANY,
        "batch_size": _ANY,
        "target_shard_rows": _ANY,
        "temp_disk_budget_bytes": _ANY,
        "compatibility_export": _ANY,
    },
    "order_pipeline": {
        "inputs": {
            "partition_gpkg": _ANY,
            "road_relation_edges_csv": _ANY,
            "order_datasets": [_ANY],
            "poi_path": _ANY,
        },
        "outputs": {"root": _ANY},
        "keep_staging_db": _ANY,
        "time_slot_minutes": _ANY,
        "order": {
            "chunksize": _ANY,
            "max_match_distance_m": _ANY,
            "start_time": _ANY,
            "end_time": _ANY,
            "order_id_column": _ANY,
            "driver_id_column": _ANY,
            "pickup_lon_column": _ANY,
            "pickup_lat_column": _ANY,
            "dropoff_lon_column": _ANY,
            "dropoff_lat_column": _ANY,
            "departure_time_column": _ANY,
            "finish_time_column": _ANY,
        },
        "poi": {
            "lon_column": _ANY,
            "lat_column": _ANY,
            "category_column": _ANY,
            "max_match_distance_m": _ANY,
            "similarity_top_k": _ANY,
        },
        "road_graph": {"weight_column": _ANY},
        "distance_graph": {"top_k": _ANY, "decay_distance_m": _ANY},
        "graph_normalization": {"add_self_loops": _ANY, "symmetric": _ANY},
    },
    "standalone": {"output_dir": _ANY},
    "contract": {"assigned_chunksize": _ANY},
    "metadata": _OPEN_MAPPING,
    "notes": _ANY,
}

_SUPPLY_SCHEMA = {
    "schema_version": _ANY,
    "dataset_config": _ANY,
    "scope": _ANY,
    "stage3_supply": {
        "orders_path": _ANY,
        "cluster_index_path": _ANY,
        "output_dir": _ANY,
        "max_gap_minutes": _ANY,
        "tau_idle_minutes": _ANY,
        "carpool_merge_gap_s": _ANY,
        "slot_duration_min": _ANY,
        "n_blocks": _ANY,
    },
    "standalone": {"output_dir": _ANY},
    "contract": {"chunksize": _ANY},
    "metadata": _OPEN_MAPPING,
    "notes": _ANY,
}

_TTE_SCHEMA = {
    "schema_version": _ANY,
    "dataset_config": _ANY,
    "scope": _ANY,
    "stage4_tte": {
        "inputs": {
            "orders_path": _ANY,
            "cluster_index_path": _ANY,
            "network_distance_path": _ANY,
            "representative_nodes_path": _ANY,
        },
        "output_dir": _ANY,
        "outputs": {
            "count_filename": _ANY,
            "hops_filename": _ANY,
            "support_filename": _ANY,
        },
        "distance": {
            "matrix_filename": _ANY,
            "representatives_filename": _ANY,
            "recompute": _ANY,
            "graphml_path": _ANY,
            "classified_edges_path": _ANY,
            "partition_gpkg": _ANY,
        },
        "time": {"freq": _ANY, "start_time": _ANY, "end_time": _ANY},
        "trip_time": {"min_minutes": _ANY, "max_minutes": _ANY, "aggregation": _ANY},
        "keep_place": {"min_origin_orders": _ANY, "min_dest_orders": _ANY},
        "imputation": {
            "method": _ANY,
            "max_hops": _ANY,
            "source_min_count": _ANY,
            "detour_ratio": _ANY,
            "speed_limit_kmh": [_ANY],
            "min_dist_km": _ANY,
            "window": _ANY,
            "outlier_std_threshold": _ANY,
            "use_validation": _ANY,
        },
    },
    "standalone": {"output_dir": _ANY},
    "contract": {"batch_size": _ANY},
    "metadata": _OPEN_MAPPING,
    "notes": _ANY,
}


def stable_value(value: Any) -> Any:
    """Convert configuration/runtime values into stable JSON-compatible data."""
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): stable_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [stable_value(item) for item in value]
    if isinstance(value, set):
        items = [stable_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported value for stable serialization: {type(value).__name__}")


def config_fingerprint(values: Mapping[str, Any]) -> str:
    payload = json.dumps(
        stable_value(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _resolve_field(values: dict[str, Any], field: str, config_dir: Path) -> None:
    parts = field.split(".")
    current: Any = values
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if not isinstance(current, dict) or parts[-1] not in current:
        return
    value = current[parts[-1]]
    if value is None:
        return
    if isinstance(value, list):
        current[parts[-1]] = [resolve_path(item, base_dir=config_dir) for item in value]
    elif isinstance(value, (str, Path)):
        current[parts[-1]] = resolve_path(value, base_dir=config_dir)
    else:
        raise TypeError(f"declared path field {field!r} must contain a path or list of paths")


def load_stage_config(
    source_path: str | Path,
    *,
    path_fields: tuple[str, ...] = (),
) -> ResolvedStageConfig:
    source = Path(source_path).expanduser().resolve()
    with source.open("r", encoding="utf-8") as handle:
        loaded = yaml.load(handle, Loader=_UniqueKeyLoader)
    if not isinstance(loaded, dict):
        raise ValueError("configuration root must be a YAML mapping")

    values = dict(loaded)
    if "project_root" in values and values["project_root"] is not None:
        values["project_root"] = resolve_path(values["project_root"], base_dir=source.parent)
    for field in path_fields:
        _resolve_field(values, field, source.parent)
    return ResolvedStageConfig(source, values, config_fingerprint(values))


def _load_yaml_mapping(source_path: str | Path) -> tuple[Path, dict[str, Any]]:
    source = Path(source_path).expanduser().resolve()
    try:
        with source.open("r", encoding="utf-8") as handle:
            loaded = yaml.load(handle, Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"{source}: unable to read YAML: {error}") from error
    if not isinstance(loaded, dict):
        raise ConfigError(f"{source}: configuration root must be a YAML mapping")
    return source, deepcopy(loaded)


def _validate_schema(value: Any, schema: Any, *, source: Path, field: str = "") -> None:
    if schema is _ANY:
        return
    if schema is _OPEN_MAPPING:
        if not isinstance(value, Mapping):
            raise ConfigError(f"{source}: {field or '<root>'} must be a mapping")
        return
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise ConfigError(f"{source}: {field or '<root>'} must be a list")
        for index, item in enumerate(value):
            _validate_schema(item, schema[0], source=source, field=f"{field}[{index}]")
        return
    if not isinstance(schema, dict) or not isinstance(value, Mapping):
        raise ConfigError(f"{source}: {field or '<root>'} has the wrong structure")
    wildcard = schema.get("*")
    for key, item in value.items():
        child_schema = schema.get(str(key), wildcard)
        child_field = f"{field}.{key}" if field else str(key)
        if child_schema is None:
            raise ConfigError(f"{source}: unknown field {child_field}")
        _validate_schema(item, child_schema, source=source, field=child_field)


def _value_at(values: Mapping[str, Any], field: str, *, source: Path) -> Any:
    current: Any = values
    for part in field.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise ConfigError(f"{source}: missing required field {field}")
        current = current[part]
    return current


def _validate_output_identifier(value: Any, *, source: Path, field: str) -> None:
    if not isinstance(value, str) or value in {".", ".."} or not _OUTPUT_IDENTIFIER.fullmatch(value):
        raise ConfigError(f"{source}: {field} must be a safe filename identifier")


def _validate_output_filename(value: Any, *, source: Path, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or Path(value).name != value
        or PureWindowsPath(value).name != value
    ):
        raise ConfigError(f"{source}: {field} must be a plain filename")


def _resolve_path_value(value: Any, *, source: Path, field: str, base_dir: Path) -> str | list[str] | None:
    if value is None:
        return None
    try:
        if isinstance(value, list):
            return [str(resolve_path(item, base_dir=base_dir)) for item in value]
        if isinstance(value, (str, Path)):
            return str(resolve_path(value, base_dir=base_dir))
    except ValueError as error:
        raise ConfigError(f"{source}: invalid path at {field}: {error}") from error
    raise ConfigError(f"{source}: {field} must be a path or list of paths")


def _resolve_pattern(values: dict[str, Any], pattern: str, *, source: Path) -> None:
    parts = pattern.split(".")

    def visit(current: Any, index: int, field: str) -> None:
        if index == len(parts):
            raise AssertionError("path pattern ended without a parent")
        part = parts[index]
        if part == "*":
            if current is None:
                return
            if not isinstance(current, dict):
                raise ConfigError(f"{source}: {field or '<root>'} must be a mapping")
            if index == len(parts) - 1:
                for key, item in list(current.items()):
                    child = f"{field}.{key}" if field else str(key)
                    current[key] = _resolve_path_value(item, source=source, field=child, base_dir=source.parent)
                return
            for key, item in current.items():
                visit(item, index + 1, f"{field}.{key}" if field else str(key))
            return
        if not isinstance(current, dict) or part not in current:
            return
        child_field = f"{field}.{part}" if field else part
        if index == len(parts) - 1:
            current[part] = _resolve_path_value(
                current[part], source=source, field=child_field, base_dir=source.parent,
            )
            return
        visit(current[part], index + 1, child_field)

    visit(values, 0, "")


def _resolve_path_mapping(values: dict[str, Any], field: str, *, source: Path) -> None:
    current = _value_at(values, field, source=source)
    if not isinstance(current, dict):
        raise ConfigError(f"{source}: {field} must be a mapping of paths")
    for key, value in list(current.items()):
        child = f"{field}.{key}"
        if isinstance(value, dict):
            _resolve_path_mapping(values, child, source=source)
        else:
            current[key] = _resolve_path_value(value, source=source, field=child, base_dir=source.parent)


def _load_dataset(stage_source: Path, stage_values: Mapping[str, Any]) -> tuple[Path, dict[str, Any], Path, str]:
    dataset_value = _value_at(stage_values, "dataset_config", source=stage_source)
    try:
        dataset_path = resolve_path(dataset_value, base_dir=stage_source.parent)
    except ValueError as error:
        raise ConfigError(f"{stage_source}: invalid path at dataset_config: {error}") from error
    dataset_source, dataset = _load_yaml_mapping(dataset_path)
    _validate_schema(dataset, _DATASET_SCHEMA, source=dataset_source)
    for field in ("schema_version", "project_root", "scope", "crs.projected", "crs.geographic", "study_area"):
        _value_at(dataset, field, source=dataset_source)
    if int(dataset["schema_version"]) != 1:
        raise ConfigError(f"{dataset_source}: unsupported schema_version {dataset['schema_version']!r}")
    try:
        project_root = resolve_path(dataset["project_root"], base_dir=dataset_source.parent)
    except ValueError as error:
        raise ConfigError(f"{dataset_source}: invalid path at project_root: {error}") from error
    dataset["project_root"] = str(project_root)
    if "paths" in dataset:
        _resolve_path_mapping(dataset, "paths", source=dataset_source)
    scope = str(dataset["scope"])
    return dataset_source, dataset, project_root, scope


def _base_resolved(
    *,
    stage: str,
    source: Path,
    stage_values: dict[str, Any],
    schema: Mapping[str, Any],
) -> tuple[dict[str, Any], Path, Path, str]:
    _validate_schema(stage_values, schema, source=source)
    for field in ("schema_version", "dataset_config", "scope"):
        _value_at(stage_values, field, source=source)
    if int(stage_values["schema_version"]) != 1:
        raise ConfigError(f"{source}: unsupported schema_version {stage_values['schema_version']!r}")
    dataset_source, dataset, project_root, dataset_scope = _load_dataset(source, stage_values)
    stage_scope = str(stage_values["scope"])
    if stage_scope != dataset_scope:
        raise ConfigError(
            f"{source}: scope={stage_scope!r} conflicts with "
            f"{dataset_source}: scope={dataset_scope!r}"
        )
    metadata = {
        "schema_version": 1,
        "stage": stage,
        "scope": stage_scope,
        "project_root": str(project_root),
        "dataset_config": str(dataset_source),
        "dataset_roots": [
            dataset["paths"][name]
            for name in ("raw_root", "interim_root", "processed_root")
            if dataset.get("paths", {}).get(name) is not None
        ],
    }
    return {"_resolved": metadata}, dataset_source, project_root, stage_scope


def _finish_resolved(
    source: Path,
    values: dict[str, Any],
    *,
    stage: str,
    scope: str,
    project_root: Path,
    dataset_path: Path,
) -> ResolvedStageConfig:
    return ResolvedStageConfig(
        source_path=source,
        values=values,
        fingerprint=config_fingerprint(values),
        stage=stage,
        scope=scope,
        project_root=project_root,
        dataset_path=dataset_path,
    )


def resolve_partition_config(path: str | Path) -> ResolvedStageConfig:
    source, raw = _load_yaml_mapping(path)
    values, dataset_path, project_root, scope = _base_resolved(
        stage="partition", source=source, stage_values=raw, schema=_PARTITION_SCHEMA,
    )
    for field in (
        "stage1_partition.graph_variant",
        "stage1_partition.regularized.initialization",
        "stage1_partition.regularized.inputs.graph",
        "stage1_partition.regularized.inputs.relation_edges",
        "stage1_partition.regularized.inputs.segment_nodes",
        "stage1_partition.regularized.inputs.order_features",
        "stage1_partition.regularized.baseline_clusters",
        "stage1_partition.regularized.objective",
        "stage1_partition.regularized.search",
    ):
        _value_at(raw, field, source=source)
    for field in (
        "stage1_partition.regularized.inputs.*",
        "stage1_partition.regularized.baseline_clusters.*",
        "stage1_partition.outputs.run_root",
        "stage1_partition.outputs.canonical_partition",
        "standalone.output_dir",
        "contract.expected_partition",
    ):
        _resolve_pattern(raw, field, source=source)
    stage = raw["stage1_partition"]
    regularized = stage["regularized"]
    _validate_output_identifier(
        stage["graph_variant"], source=source, field="stage1_partition.graph_variant",
    )
    _validate_output_identifier(
        regularized["initialization"],
        source=source,
        field="stage1_partition.regularized.initialization",
    )
    if regularized["initialization"] not in regularized["baseline_clusters"]:
        raise ConfigError(
            f"{source}: stage1_partition.regularized.baseline_clusters."
            f"{regularized['initialization']} is required"
        )
    standalone = raw.get("standalone", {})
    output_root = standalone.get("output_dir", stage.get("outputs", {}).get("run_root"))
    if output_root is None:
        raise ConfigError(f"{source}: missing required field standalone.output_dir")
    values.update({
        "scope": {"active": scope, "graph_variant": stage["graph_variant"]},
        "inputs": {
            **deepcopy(regularized["inputs"]),
            "baseline_clusters": deepcopy(regularized["baseline_clusters"]),
        },
        "outputs": {"root": output_root, "overwrite": True, "resume": False},
        "initializations": [regularized["initialization"]],
        "objective": deepcopy(regularized["objective"]),
        "search": deepcopy(regularized["search"]),
        "contract": {
            **deepcopy(raw.get("contract", {})),
            "expected_partition": raw.get("contract", {}).get(
                "expected_partition",
                stage.get("outputs", {}).get("canonical_partition"),
            ),
        },
    })
    return _finish_resolved(
        source, values, stage="partition", scope=scope,
        project_root=project_root, dataset_path=dataset_path,
    )


def _dataset_study_area(dataset: Mapping[str, Any], scope: str) -> dict[str, Any]:
    study_area = deepcopy(dict(dataset["study_area"]))
    configured = study_area.get("active")
    if configured is not None and str(configured) != scope:
        raise ConfigError(f"dataset study_area.active={configured!r} conflicts with scope={scope!r}")
    study_area["active"] = scope
    return study_area


def resolve_demand_config(path: str | Path) -> ResolvedStageConfig:
    source, raw = _load_yaml_mapping(path)
    values, dataset_path, project_root, scope = _base_resolved(
        stage="demand", source=source, stage_values=raw, schema=_DEMAND_SCHEMA,
    )
    for field in (
        "order_pipeline.inputs.partition_gpkg",
        "order_pipeline.inputs.road_relation_edges_csv",
        "order_pipeline.inputs.order_datasets",
        "order_pipeline.inputs.poi_path",
        "order_pipeline.time_slot_minutes",
        "order_pipeline.order",
        "order_pipeline.poi",
        "order_pipeline.road_graph",
        "order_pipeline.distance_graph",
        "order_pipeline.graph_normalization",
    ):
        _value_at(raw, field, source=source)
    for field in (
        "order_pipeline.inputs.partition_gpkg",
        "order_pipeline.inputs.road_relation_edges_csv",
        "order_pipeline.inputs.order_datasets",
        "order_pipeline.inputs.poi_path",
        "order_pipeline.outputs.root",
        "standalone.output_dir",
    ):
        _resolve_pattern(raw, field, source=source)
    _, dataset = _load_yaml_mapping(dataset_path)
    pipeline = deepcopy(raw["order_pipeline"])
    gzip_compresslevel = validate_gzip_compresslevel(
        raw.get("gzip_compresslevel", DEFAULT_GZIP_COMPRESSLEVEL), source=source,
    )
    order_staging_backend, order_staging = validate_order_staging(
        raw.get("order_staging_backend"), raw.get("order_staging"), source=source,
    )
    if raw.get("standalone", {}).get("output_dir") is not None:
        pipeline["outputs"] = {**dict(pipeline.get("outputs", {})), "root": raw["standalone"]["output_dir"]}
    values.update({
        "study_area": _dataset_study_area(dataset, scope),
        "crs": deepcopy(dataset["crs"]),
        "order_pipeline": pipeline,
        "gzip_compresslevel": gzip_compresslevel,
        "order_staging_backend": order_staging_backend,
        "order_staging": order_staging,
        "contract": deepcopy(raw.get("contract", {})),
    })
    return _finish_resolved(
        source, values, stage="demand", scope=scope,
        project_root=project_root, dataset_path=dataset_path,
    )


def resolve_supply_config(path: str | Path) -> ResolvedStageConfig:
    source, raw = _load_yaml_mapping(path)
    values, dataset_path, project_root, scope = _base_resolved(
        stage="supply", source=source, stage_values=raw, schema=_SUPPLY_SCHEMA,
    )
    for field in (
        "stage3_supply.orders_path",
        "stage3_supply.max_gap_minutes",
        "stage3_supply.tau_idle_minutes",
        "stage3_supply.carpool_merge_gap_s",
        "stage3_supply.slot_duration_min",
        "stage3_supply.n_blocks",
    ):
        _value_at(raw, field, source=source)
    for field in (
        "stage3_supply.orders_path",
        "stage3_supply.cluster_index_path",
        "stage3_supply.output_dir",
        "standalone.output_dir",
    ):
        _resolve_pattern(raw, field, source=source)
    stage = deepcopy(raw["stage3_supply"])
    if raw.get("standalone", {}).get("output_dir") is not None:
        stage["output_dir"] = raw["standalone"]["output_dir"]
    if int(stage["n_blocks"]) <= 0:
        raise ConfigError(f"{source}: stage3_supply.n_blocks must be positive")
    values.update({"stage3_supply": stage, "contract": deepcopy(raw.get("contract", {}))})
    return _finish_resolved(
        source, values, stage="supply", scope=scope,
        project_root=project_root, dataset_path=dataset_path,
    )


def resolve_tte_config(path: str | Path) -> ResolvedStageConfig:
    source, raw = _load_yaml_mapping(path)
    values, dataset_path, project_root, scope = _base_resolved(
        stage="tte", source=source, stage_values=raw, schema=_TTE_SCHEMA,
    )
    for field in (
        "stage4_tte.inputs.orders_path",
        "stage4_tte.inputs.cluster_index_path",
        "stage4_tte.time.freq",
        "stage4_tte.time.start_time",
        "stage4_tte.time.end_time",
        "stage4_tte.trip_time",
        "stage4_tte.imputation",
    ):
        _value_at(raw, field, source=source)
    for field in (
        "stage4_tte.inputs.orders_path",
        "stage4_tte.inputs.cluster_index_path",
        "stage4_tte.inputs.network_distance_path",
        "stage4_tte.inputs.representative_nodes_path",
        "stage4_tte.output_dir",
        "stage4_tte.distance.graphml_path",
        "stage4_tte.distance.classified_edges_path",
        "stage4_tte.distance.partition_gpkg",
        "standalone.output_dir",
    ):
        _resolve_pattern(raw, field, source=source)
    fallback = raw["stage4_tte"]["inputs"]
    for (section, name), expected in _TTE_FILENAMES.items():
        value = raw["stage4_tte"].get(section, {}).get(name)
        if value is not None:
            _validate_output_filename(
                value, source=source, field=f"stage4_tte.{section}.{name}",
            )
            if value != expected:
                raise ConfigError(
                    f"{source}: stage4_tte.{section}.{name} must be {expected!r}"
                )
    has_matrix = fallback.get("network_distance_path") is not None
    has_representatives = fallback.get("representative_nodes_path") is not None
    if has_matrix != has_representatives:
        raise ConfigError(
            f"{source}: stage4_tte.inputs.network_distance_path and "
            "representative_nodes_path must be provided together"
        )
    _, dataset = _load_yaml_mapping(dataset_path)
    stage = deepcopy(raw["stage4_tte"])
    if raw.get("standalone", {}).get("output_dir") is not None:
        stage["output_dir"] = raw["standalone"]["output_dir"]
    values.update({
        "study_area": _dataset_study_area(dataset, scope),
        "crs": deepcopy(dataset["crs"]),
        "stage4_tte": stage,
        "contract": deepcopy(raw.get("contract", {})),
    })
    return _finish_resolved(
        source, values, stage="tte", scope=scope,
        project_root=project_root, dataset_path=dataset_path,
    )


def apply_stage_overrides(
    config: ResolvedStageConfig,
    overrides: Mapping[str, Any] | None,
) -> ResolvedStageConfig:
    if not overrides:
        return config
    stage = config.stage
    allowed = {"supply": {"n_blocks"}}
    unknown = set(overrides) - allowed.get(stage or "", set())
    if unknown:
        raise ConfigError(f"{config.source_path}: unsupported {stage or 'stage'} CLI overrides: {sorted(unknown)}")
    values = deepcopy(dict(config.values))
    if stage == "supply" and "n_blocks" in overrides:
        n_blocks = int(overrides["n_blocks"])
        if n_blocks <= 0:
            raise ConfigError("CLI --n-blocks must be positive")
        values["stage3_supply"] = {**dict(values["stage3_supply"]), "n_blocks": n_blocks}
    values["_resolved"] = {
        **dict(values["_resolved"]),
        "overrides": {key: overrides[key] for key in sorted(overrides)},
    }
    return ResolvedStageConfig(
        source_path=config.source_path,
        values=values,
        fingerprint=config_fingerprint(values),
        stage=config.stage,
        scope=config.scope,
        project_root=config.project_root,
        dataset_path=config.dataset_path,
    )
