from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

import roadnet_partition.config as config_module
from roadnet_partition.config import (
    ConfigError,
    ResolvedStageConfig,
    apply_stage_overrides,
    config_fingerprint,
    resolve_demand_config,
    resolve_partition_config,
    resolve_supply_config,
    resolve_tte_config,
)
from roadnet_partition.zoning.partition import build_regularized_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET = PROJECT_ROOT / "configs/datasets/fifth_ring.yaml"
PRODUCTION = {
    "partition": (resolve_partition_config, PROJECT_ROOT / "configs/zoning/regularized.yaml"),
    "demand": (resolve_demand_config, PROJECT_ROOT / "configs/pipelines/demand.yaml"),
    "supply": (resolve_supply_config, PROJECT_ROOT / "configs/pipelines/supply.yaml"),
    "tte": (resolve_tte_config, PROJECT_ROOT / "configs/pipelines/tte.yaml"),
}
PATH_FIELDS = {
    "partition": (
        "_resolved.project_root", "_resolved.dataset_config", "_resolved.dataset_roots", "inputs.graph",
        "inputs.relation_edges", "inputs.classified_edges", "inputs.boundary",
        "inputs.segment_nodes", "inputs.poi_features", "inputs.order_features",
        "inputs.hourly_od", "inputs.baseline_clusters", "outputs.root",
        "contract.expected_partition",
    ),
    "demand": (
        "_resolved.project_root", "_resolved.dataset_config", "_resolved.dataset_roots",
        "order_pipeline.inputs.partition_gpkg",
        "order_pipeline.inputs.road_relation_edges_csv",
        "order_pipeline.inputs.order_datasets", "order_pipeline.inputs.poi_path",
        "order_pipeline.outputs.root",
    ),
    "supply": (
        "_resolved.project_root", "_resolved.dataset_config", "_resolved.dataset_roots", "stage3_supply.orders_path",
        "stage3_supply.cluster_index_path", "stage3_supply.output_dir",
    ),
    "tte": (
        "_resolved.project_root", "_resolved.dataset_config", "_resolved.dataset_roots",
        "stage4_tte.inputs.orders_path", "stage4_tte.inputs.cluster_index_path",
        "stage4_tte.inputs.network_distance_path",
        "stage4_tte.inputs.representative_nodes_path", "stage4_tte.output_dir",
        "stage4_tte.distance.graphml_path", "stage4_tte.distance.classified_edges_path",
        "stage4_tte.distance.partition_gpkg",
    ),
}


def _at(values: Any, field: str) -> Any:
    current = values
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _paths(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [str(item) for item in value.values() if item is not None]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


@pytest.mark.parametrize("stage", PRODUCTION)
def test_production_resolvers_are_cwd_independent_and_resolve_declared_paths(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, path = PRODUCTION[stage]
    monkeypatch.chdir(tmp_path)
    resolved = resolver(path)

    assert resolved.stage == stage
    assert resolved.scope == "fifth_ring"
    assert resolved.source_path == path.resolve()
    assert resolved.dataset_path == DATASET.resolve()
    assert resolved.project_root == PROJECT_ROOT.resolve()
    for field in PATH_FIELDS[stage]:
        for value in _paths(_at(resolved.values, field)):
            assert Path(value).is_absolute(), f"{stage}: {field} remained relative: {value}"


@pytest.mark.parametrize("dataset_name", ["fifth_ring.yaml", "fourth_ring.yaml"])
def test_production_dataset_paths_resolve_from_dataset_file(
    dataset_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_path = PROJECT_ROOT / "configs/datasets" / dataset_name
    stage_source = tmp_path / "elsewhere/stage.yaml"
    monkeypatch.chdir(tmp_path)
    source, dataset, project_root, scope = config_module._load_dataset(
        stage_source,
        {"dataset_config": str(dataset_path)},
    )

    assert source == dataset_path.resolve()
    assert project_root == PROJECT_ROOT.resolve()
    assert scope == dataset_name.removesuffix(".yaml")
    for field, value in dataset["paths"].items():
        assert Path(value).is_absolute(), f"dataset paths.{field} remained relative: {value}"
    assert Path(dataset["paths"]["raw_root"]) == (dataset_path.parent / "../../data/raw").resolve()


def test_dataset_config_is_stage_relative_and_dataset_values_are_dataset_relative(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "datasets"
    stage_dir = tmp_path / "stages"
    dataset_dir.mkdir()
    stage_dir.mkdir()
    dataset_path = dataset_dir / "tiny.yaml"
    dataset_path.write_text(
        yaml.safe_dump({
            "schema_version": 1,
            "project_root": "../project",
            "scope": "tiny",
            "crs": {"projected": "EPSG:3857", "geographic": "EPSG:4326"},
            "study_area": {},
            "paths": {"raw_root": "assets/raw"},
        }),
        encoding="utf-8",
    )
    stage_path = stage_dir / "supply.yaml"
    stage_values = {
        "schema_version": 1,
        "dataset_config": "../datasets/tiny.yaml",
        "scope": "tiny",
        "stage3_supply": {
            "orders_path": "inputs/orders.csv",
            "max_gap_minutes": 60,
            "tau_idle_minutes": 30,
            "carpool_merge_gap_s": 0,
            "slot_duration_min": 10,
            "n_blocks": 2,
        },
    }
    stage_path.write_text(yaml.safe_dump(stage_values), encoding="utf-8")

    resolved = resolve_supply_config(stage_path)
    _, dataset, _, _ = config_module._load_dataset(stage_path, stage_values)

    assert resolved.dataset_path == dataset_path.resolve()
    assert resolved.project_root == (dataset_dir / "../project").resolve()
    assert Path(resolved.values["stage3_supply"]["orders_path"]) == (stage_dir / "inputs/orders.csv").resolve()
    assert Path(dataset["paths"]["raw_root"]) == (dataset_dir / "assets/raw").resolve()


def _production_copy(tmp_path: Path, stage: str) -> tuple[Callable[[Path], ResolvedStageConfig], Path, dict[str, Any]]:
    resolver, source = PRODUCTION[stage]
    values = yaml.safe_load(source.read_text(encoding="utf-8"))
    values["dataset_config"] = str(DATASET)
    destination = tmp_path / f"{stage}.yaml"
    return resolver, destination, values


def test_scope_mismatch_reports_both_configs(tmp_path: Path) -> None:
    resolver, path, values = _production_copy(tmp_path, "supply")
    values["scope"] = "fourth_ring"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        resolver(path)
    message = str(caught.value)
    assert str(path) in message
    assert str(DATASET) in message
    assert "fourth_ring" in message and "fifth_ring" in message


@pytest.mark.parametrize(
    ("stage", "missing_field"),
    [
        ("partition", "stage1_partition.regularized.inputs.graph"),
        ("demand", "order_pipeline.order"),
        ("supply", "stage3_supply.n_blocks"),
        ("tte", "stage4_tte.time.freq"),
    ],
)
def test_each_resolver_rejects_missing_required_field(
    stage: str,
    missing_field: str,
    tmp_path: Path,
) -> None:
    resolver, path, values = _production_copy(tmp_path, stage)
    current = values
    parts = missing_field.split(".")
    for part in parts[:-1]:
        current = current[part]
    del current[parts[-1]]
    path.write_text(yaml.safe_dump(values), encoding="utf-8")

    with pytest.raises(ConfigError, match=missing_field):
        resolver(path)


@pytest.mark.parametrize("stage", PRODUCTION)
def test_each_resolver_rejects_unknown_field(stage: str, tmp_path: Path) -> None:
    resolver, path, values = _production_copy(tmp_path, stage)
    values["formal_paramter_typo"] = 1
    path.write_text(yaml.safe_dump(values), encoding="utf-8")

    with pytest.raises(ConfigError, match="unknown field formal_paramter_typo"):
        resolver(path)


@pytest.mark.skipif(os.name == "nt", reason="Linux-specific foreign Windows path check")
def test_windows_path_string_is_rejected_on_linux(tmp_path: Path) -> None:
    resolver, path, values = _production_copy(tmp_path, "supply")
    values["stage3_supply"]["orders_path"] = r"C:\private\orders.csv"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")

    with pytest.raises(ConfigError, match="Windows path"):
        resolver(path)


@pytest.mark.skipif(os.name == "nt", reason="Linux-specific foreign Windows path check")
def test_relative_windows_path_string_is_rejected_on_linux(tmp_path: Path) -> None:
    resolver, path, values = _production_copy(tmp_path, "supply")
    values["stage3_supply"]["orders_path"] = r"..\private\orders.csv"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")

    with pytest.raises(ConfigError, match="Windows path"):
        resolver(path)


def test_duplicate_yaml_key_and_merge_are_rejected(tmp_path: Path) -> None:
    resolver, source = PRODUCTION["supply"]
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(source.read_text(encoding="utf-8") + "\nscope: other\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key 'scope'"):
        resolver(duplicate)

    merged = tmp_path / "merged.yaml"
    merged.write_text("<<: {metadata: {source: merged}}\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML merge keys are not supported"):
        resolver(merged)


@pytest.mark.parametrize(
    ("stage", "field", "value"),
    [
        ("partition", "stage1_partition.graph_variant", "../escape"),
        ("partition", "stage1_partition.regularized.initialization", "leiden/escape"),
        ("tte", "stage4_tte.outputs.count_filename", "../escape.parquet"),
        ("tte", "stage4_tte.distance.matrix_filename", r"..\escape.parquet"),
        ("tte", "stage4_tte.outputs.count_filename", "custom.parquet"),
    ],
)
def test_filename_producing_config_fields_reject_path_traversal(
    stage: str,
    field: str,
    value: str,
    tmp_path: Path,
) -> None:
    resolver, path, values = _production_copy(tmp_path, stage)
    current = values
    parts = field.split(".")
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value
    path.write_text(yaml.safe_dump(values), encoding="utf-8")

    with pytest.raises(ConfigError, match=field):
        resolver(path)


def test_comments_and_key_order_do_not_change_fingerprint(tmp_path: Path) -> None:
    _, source = PRODUCTION["supply"]
    values = yaml.safe_load(source.read_text(encoding="utf-8"))
    values["dataset_config"] = str(DATASET)
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    second.write_text("# same effective config\n" + yaml.safe_dump(values, sort_keys=True), encoding="utf-8")

    assert resolve_supply_config(first).fingerprint == resolve_supply_config(second).fingerprint


def test_supply_n_blocks_override_is_resolved_fingerprinted_and_allowlisted() -> None:
    original = resolve_supply_config(PRODUCTION["supply"][1])
    overridden = apply_stage_overrides(original, {"n_blocks": 3})

    assert original.values["stage3_supply"]["n_blocks"] == 8
    assert overridden.values["stage3_supply"]["n_blocks"] == 3
    assert overridden.values["_resolved"]["overrides"] == {"n_blocks": 3}
    assert overridden.fingerprint == config_fingerprint(overridden.values)
    assert overridden.fingerprint != original.fingerprint
    with pytest.raises(ConfigError, match="unsupported supply CLI overrides"):
        apply_stage_overrides(original, {"workers": 2})


def _absolute_project_paths(values: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result = deepcopy(values)
    for field in fields:
        current = result
        parts = field.split(".")
        for part in parts[:-1]:
            current = current[part]
        value = current[parts[-1]]
        if isinstance(value, list):
            current[parts[-1]] = [str((PROJECT_ROOT / item).resolve()) for item in value]
        elif isinstance(value, dict):
            current[parts[-1]] = {key: str((PROJECT_ROOT / item).resolve()) for key, item in value.items()}
        elif value is not None:
            current[parts[-1]] = str((PROJECT_ROOT / value).resolve())
    return result


def _same_shape(actual: Any, expected: Any) -> Any:
    if isinstance(expected, dict):
        return {key: _same_shape(actual[key], value) for key, value in expected.items()}
    return actual


def _mask_fields(values: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result = deepcopy(values)
    for field in fields:
        current = result
        parts = field.split(".")
        for part in parts[:-1]:
            current = current[part]
        current[parts[-1]] = "<phase8-path>"
    return result


def test_production_effective_algorithm_parameters_match_root_config() -> None:
    root = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))

    partition = resolve_partition_config(PRODUCTION["partition"][1])
    expected_partition = build_regularized_config(root, str(partition.values["outputs"]["root"]))
    expected_partition = {
        key: value for key, value in expected_partition.items()
        if key not in {"baseline", "evaluation", "visualization"}
    }
    expected_partition = _absolute_project_paths(
        expected_partition,
        ("inputs.graph", "inputs.relation_edges", "inputs.classified_edges", "inputs.boundary",
         "inputs.segment_nodes", "inputs.poi_features", "inputs.order_features", "inputs.hourly_od",
         "inputs.baseline_clusters"),
    )
    actual_partition = {key: partition.values[key] for key in expected_partition}
    assert _mask_fields(actual_partition, ("inputs", "outputs")) == _mask_fields(
        expected_partition, ("inputs", "outputs"),
    )

    demand = resolve_demand_config(PRODUCTION["demand"][1])
    expected_demand = _absolute_project_paths(
        root["order_pipeline"],
        ("inputs.partition_gpkg", "inputs.road_relation_edges_csv", "inputs.order_datasets", "inputs.poi_path"),
    )
    assert _mask_fields(demand.values["order_pipeline"], ("inputs", "outputs")) == _mask_fields(
        expected_demand, ("inputs", "outputs"),
    )

    supply = resolve_supply_config(PRODUCTION["supply"][1])
    expected_supply = {
        key: value for key, value in root["stage3_supply"].items()
        if key not in {"demand_path", "demand_dir", "peak_morning_hours", "peak_evening_hours"}
    }
    expected_supply = _absolute_project_paths(expected_supply, ("orders_path", "output_dir"))
    supply_paths = ("orders_path", "cluster_index_path", "output_dir")
    assert _mask_fields(supply.values["stage3_supply"], supply_paths) == _mask_fields(
        expected_supply, supply_paths,
    )

    tte = resolve_tte_config(PRODUCTION["tte"][1])
    expected_tte = _absolute_project_paths(
        root["stage4_tte"],
        ("inputs.orders_path", "inputs.cluster_index_path", "output_dir", "distance.graphml_path",
         "distance.classified_edges_path", "distance.partition_gpkg"),
    )
    actual_tte = _same_shape(tte.values["stage4_tte"], expected_tte)
    tte_paths = (
        "inputs.orders_path", "inputs.cluster_index_path", "inputs.network_distance_path",
        "inputs.representative_nodes_path", "output_dir", "distance.graphml_path",
        "distance.classified_edges_path", "distance.partition_gpkg",
    )
    assert _mask_fields(actual_tte, tte_paths) == _mask_fields(expected_tte, tte_paths)
