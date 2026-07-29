"""Build the Phase 6A production-config audit and equivalence reports."""

from __future__ import annotations

import argparse
from collections import Counter
import html
import json
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
ROOT_CONFIG = ROOT / "config.yaml"
KEY_MAP = ROOT / "docs/refactor/config-key-map-v1.json"
AUDIT_REPORT = ROOT / "docs/refactor/production-config-split-v1.md"
EQUIVALENCE_REPORT = ROOT / "docs/refactor/production-config-equivalence-v1.md"

SPLIT_FILES = {
    "fifth": ROOT / "configs/datasets/fifth_ring.yaml",
    "fourth": ROOT / "configs/datasets/fourth_ring.yaml",
    "partition": ROOT / "configs/zoning/regularized.yaml",
    "demand": ROOT / "configs/pipelines/demand.yaml",
    "supply": ROOT / "configs/pipelines/supply.yaml",
    "tte": ROOT / "configs/pipelines/tte.yaml",
}

# These are path-valued source leaves, including seven that the Phase 1 map did
# not classify as paths. Output basenames are deliberately absent.
PATH_KEYS = {
    "study_area.scopes.fifth_ring.raw_edges_path",
    "study_area.scopes.fifth_ring.raw_nodes_path",
    "study_area.scopes.fifth_ring.graphml_path",
    "study_area.scopes.fifth_ring.boundary_path",
    "study_area.scopes.fifth_ring.ring_segments_path",
    "study_area.scopes.fourth_ring.raw_edges_path",
    "study_area.scopes.fourth_ring.raw_nodes_path",
    "study_area.scopes.fourth_ring.graphml_path",
    "study_area.scopes.fourth_ring.boundary_path",
    "study_area.scopes.fourth_ring.ring_segments_path",
    "semantic_graph.poi.input_path",
    "semantic_graph.order.input_path",
    "order_pipeline.inputs.partition_gpkg",
    "order_pipeline.inputs.road_relation_edges_csv",
    "order_pipeline.inputs.order_datasets",
    "order_pipeline.inputs.poi_path",
    "order_pipeline.outputs.root",
    "stage1_partition.regularized.inputs.graph",
    "stage1_partition.regularized.inputs.relation_edges",
    "stage1_partition.regularized.inputs.classified_edges",
    "stage1_partition.regularized.inputs.boundary",
    "stage1_partition.regularized.inputs.segment_nodes",
    "stage1_partition.regularized.inputs.poi_features",
    "stage1_partition.regularized.inputs.order_features",
    "stage1_partition.regularized.inputs.hourly_od",
    "stage1_partition.regularized.baseline_clusters.louvain",
    "stage1_partition.regularized.baseline_clusters.leiden",
    "stage1_partition.regularized.baseline_clusters.demand_region_growing",
    "stage1_partition.regularized.visualization.output_dir",
    "stage1_partition.outputs.run_root",
    "stage1_partition.outputs.canonical_partition",
    "stage3_supply.orders_path",
    "stage3_supply.output_dir",
    "stage3_supply.demand_path",
    "stage3_supply.demand_dir",
    "stage4_tte.inputs.orders_path",
    "stage4_tte.inputs.cluster_index_path",
    "stage4_tte.output_dir",
    "stage4_tte.distance.graphml_path",
    "stage4_tte.distance.classified_edges_path",
    "stage4_tte.distance.partition_gpkg",
}

MAP_MISSED_PATHS = {
    "order_pipeline.inputs.partition_gpkg",
    "order_pipeline.inputs.road_relation_edges_csv",
    "order_pipeline.inputs.order_datasets",
    "stage1_partition.regularized.baseline_clusters.louvain",
    "stage1_partition.regularized.baseline_clusters.leiden",
    "stage1_partition.regularized.baseline_clusters.demand_region_growing",
    "stage4_tte.distance.partition_gpkg",
}

SCOPE_PATH_NAMES = {
    "raw_edges_path": "raw_edges",
    "raw_nodes_path": "raw_nodes",
    "graphml_path": "graphml",
    "boundary_path": "boundary",
    "ring_segments_path": "ring_segments",
}

LEGACY_ROOTS = {
    "road_filter",
    "connector_rules",
    "continuity",
    "graph_weights",
    "semantic_graph",
    "evaluation",
    "clustering",
    "visualization",
}

PARTITION_RETAINED_PREFIXES = (
    "stage1_partition.baseline",
    "stage1_partition.regularized.evaluation",
    "stage1_partition.regularized.visualization",
)

SUPPLY_RETAINED = {
    "stage3_supply.demand_path",
    "stage3_supply.demand_dir",
    "stage3_supply.peak_morning_hours",
    "stage3_supply.peak_evening_hours",
}

OPTIONAL_KEYS = {
    "order_pipeline.inputs.road_relation_edges_csv",
    "order_pipeline.inputs.poi_path",
    "order_pipeline.outputs.root",
    "order_pipeline.keep_staging_db",
    "stage1_partition.outputs.run_root",
    "stage1_partition.outputs.canonical_partition",
    "stage3_supply.output_dir",
    "stage3_supply.tau_idle_minutes",
    "stage3_supply.n_blocks",
    "stage4_tte.output_dir",
    "stage4_tte.outputs.count_filename",
    "stage4_tte.outputs.hops_filename",
    "stage4_tte.outputs.support_filename",
    "stage4_tte.distance.matrix_filename",
    "stage4_tte.distance.representatives_filename",
    "stage4_tte.distance.recompute",
    "stage4_tte.trip_time.aggregation",
    "stage4_tte.keep_place.min_origin_orders",
    "stage4_tte.keep_place.min_dest_orders",
    "stage4_tte.imputation.max_hops",
    "stage4_tte.imputation.source_min_count",
    "stage4_tte.imputation.detour_ratio",
    "stage4_tte.imputation.speed_limit_kmh",
    "stage4_tte.imputation.min_dist_km",
    "stage4_tte.imputation.window",
    "stage4_tte.imputation.outlier_std_threshold",
    "stage4_tte.imputation.use_validation",
}


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"YAML root is not a mapping: {path}")
    return value


def mapping_keys(value: object, prefix: tuple[str, ...] = ()) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = prefix + (str(key),)
            paths.append(".".join(path))
            paths.extend(mapping_keys(child, path))
    return paths


def get_path(value: Any, key_path: str) -> Any:
    current = value
    for part in key_path.split("."):
        current = current[part]
    return current


def route(key_path: str) -> tuple[str, tuple[Path, ...], str]:
    root = key_path.split(".", 1)[0]
    if key_path == "study_area":
        return "Dataset", (SPLIT_FILES["fifth"], SPLIT_FILES["fourth"]), "study_area"
    if key_path == "study_area.active":
        return "Dataset", (SPLIT_FILES["fifth"],), "scope"
    if key_path == "study_area.center_point" or key_path.startswith("study_area.center_point."):
        return "Dataset", (SPLIT_FILES["fifth"], SPLIT_FILES["fourth"]), key_path
    if key_path == "study_area.scopes":
        return "Dataset", (SPLIT_FILES["fifth"], SPLIT_FILES["fourth"]), "study_area"
    for scope_name, file_key in (("fifth_ring", "fifth"), ("fourth_ring", "fourth")):
        prefix = f"study_area.scopes.{scope_name}"
        if key_path == prefix:
            return "Dataset", (SPLIT_FILES[file_key],), "study_area"
        if key_path.startswith(prefix + "."):
            suffix = key_path[len(prefix) + 1 :]
            if suffix in SCOPE_PATH_NAMES:
                return "Dataset", (SPLIT_FILES[file_key],), f"paths.{SCOPE_PATH_NAMES[suffix]}"
            return "Dataset", (SPLIT_FILES[file_key],), f"study_area.{suffix}"
    if root == "crs":
        return "Dataset", (SPLIT_FILES["fifth"], SPLIT_FILES["fourth"]), key_path
    if old_only(key_path):
        return "Legacy / experiment", (ROOT_CONFIG,), key_path
    if root == "stage1_partition":
        return "Partition", (SPLIT_FILES["partition"],), key_path
    if root == "order_pipeline":
        return "Demand", (SPLIT_FILES["demand"],), key_path
    if root == "stage3_supply":
        return "Supply", (SPLIT_FILES["supply"],), key_path
    if root == "stage4_tte":
        return "TTE", (SPLIT_FILES["tte"],), key_path
    raise KeyError(f"no split route for {key_path}")


def relative_name(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def format_default(value: Any) -> str:
    if isinstance(value, dict):
        return f"mapping ({len(value)} direct keys)"
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return html.escape(text).replace("|", "\\|")


def current_readers(entry: dict[str, Any]) -> str:
    readers = entry.get("readers", [])
    if not readers:
        return "未确认"
    value = "<br>".join(f"{reader['file']}::{reader['function']}" for reader in readers)
    if entry.get("reader_evidence_truncated"):
        value += "<br>（证据截断）"
    return value


def new_reader(section: str, key_path: str) -> str:
    if section == "Dataset":
        return "resolve_*_config（显式 dataset adapter）"
    if section == "Partition":
        return "resolve_partition_config → run_partition"
    if section == "Demand":
        return "resolve_demand_config → run_demand"
    if section == "Supply":
        return "resolve_supply_config → run_supply"
    if section == "TTE":
        if key_path == "stage4_tte.imputation.method":
            return "resolve_tte_config（保留）；当前 TTE 算法不读取"
        return "resolve_tte_config → run_tte"
    return "—（继续由旧入口读取）"


def old_only(key_path: str) -> bool:
    root = key_path.split(".", 1)[0]
    return (
        root in LEGACY_ROOTS
        or key_path in SUPPLY_RETAINED
        or any(key_path == prefix or key_path.startswith(prefix + ".") for prefix in PARTITION_RETAINED_PREFIXES)
    )


def category(entry: dict[str, Any], key_path: str) -> str:
    if key_path.startswith("stage1_partition.regularized.visualization"):
        return "experiment / visualization"
    if key_path.startswith("stage1_partition.regularized.evaluation"):
        return "experiment / evaluation"
    if key_path.startswith("stage1_partition.baseline"):
        return "legacy baseline"
    if key_path in SUPPLY_RETAINED:
        return "legacy Supply field"
    if entry["usage"] == "legacy_00_05":
        return "legacy preprocessing"
    if entry["usage"] == "legacy_baseline":
        return "legacy baseline/reporting"
    if entry["domain"] == "dataset":
        return "dataset"
    return f"stage / {entry['domain']}"


def formal_use(entry: dict[str, Any], key_path: str) -> str:
    if old_only(key_path):
        return "否"
    if key_path == "stage4_tte.imputation.method":
        return "否（声明但未读取）"
    if entry["node_kind"] == "mapping":
        return "结构"
    if key_path == "study_area.active" or key_path.startswith("crs."):
        return "是（scope/CRS）"
    return "是" if entry["formally_used_by_four_stages"] else "否（dataset/preprocess）"


def requirement(entry: dict[str, Any], key_path: str) -> str:
    if entry["node_kind"] == "mapping":
        return "结构"
    if old_only(key_path):
        return "保留；正式 runner 不要求"
    if not entry.get("readers"):
        return "保留；reader 待确认"
    if key_path in OPTIONAL_KEYS:
        return "可选/有 fallback"
    return "必需"


def unit(entry: dict[str, Any], key_path: str) -> str:
    if entry["node_kind"] == "mapping":
        return "n/a"
    if key_path in PATH_KEYS:
        return "path"
    name = key_path.rsplit(".", 1)[-1]
    if name in {"lon", "lat", "north", "south", "east", "west"}:
        return "degree"
    if name.endswith("_km2"):
        return "km²"
    if name == "speed_limit_kmh":
        return "km/h"
    if name.endswith("_km"):
        return "km"
    if name.endswith("_m"):
        return "m"
    if name.endswith("_minutes") or name.endswith("_min"):
        return "min"
    if name.endswith("_s"):
        return "s"
    if name.endswith("_deg"):
        return "degree"
    if name.endswith("_hours"):
        return "hour-of-day"
    if name.endswith("_time"):
        return "timestamp"
    if name == "freq":
        return "pandas frequency"
    if name == "chunksize":
        return "rows/chunk"
    if name.endswith("_dpi") or name == "dpi":
        return "dpi"
    if name.endswith("_px"):
        return "pixel"
    if name.endswith("_column"):
        return "column name"
    if name.endswith("_filename"):
        return "basename"
    if "ratio" in name or "weight" in name or "alpha" in name or "lambda" in name or name == "resolution":
        return "dimensionless"
    if entry["value_type"] in {"int", "float"}:
        return "count / dimensionless"
    return "n/a"


def cli_override(key_path: str) -> str:
    if key_path == "stage3_supply.n_blocks":
        return "--n-blocks"
    return "—"


def duplicate_status(files: tuple[Path, ...]) -> str:
    if files == (ROOT_CONFIG,):
        return "否；仅根 config.yaml"
    if len(files) > 1:
        return "根配置兼容重复 + 两个 dataset 身份副本；值须一致"
    return "仅与根 config.yaml 兼容重复；split 内单一 owner"


def normalize_path_value(value: Any, base_dir: Path) -> Any:
    if value is None:
        return None
    if isinstance(value, list):
        return [(base_dir / item).expanduser().resolve() if not Path(item).expanduser().is_absolute() else Path(item).expanduser().resolve() for item in value]
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base_dir / path).resolve()


def validate_and_compare(
    root_config: dict[str, Any],
    entries: list[dict[str, Any]],
    split_values: dict[Path, dict[str, Any]],
) -> dict[str, Any]:
    source_keys = mapping_keys(root_config)
    mapped_keys = [entry["key_path"] for entry in entries]
    if len(source_keys) != 341 or len(mapped_keys) != 341 or len(set(mapped_keys)) != 341:
        raise AssertionError("the source/key-map inventory is not exactly 341 unique mapping keys")
    if sorted(source_keys) != sorted(mapped_keys):
        raise AssertionError("config-key-map-v1.json does not cover the current config.yaml")
    if sum(not entry.get("readers") for entry in entries) != 30:
        raise AssertionError("the preserved no-reader inventory is not 30")

    equal = 0
    path_equal = 0
    routed_leaves = 0
    raw_differences: list[str] = []
    for entry in entries:
        key_path = entry["key_path"]
        if entry["node_kind"] == "mapping":
            continue
        section, files, new_path = route(key_path)
        if section == "Legacy / experiment":
            continue
        routed_leaves += 1
        old_value = get_path(root_config, key_path)
        expected = [False] if key_path == "stage1_partition.regularized.search.grid.merge_split_enabled" else old_value
        if expected != old_value:
            raw_differences.append(key_path)
        for file_path in files:
            new_value = get_path(split_values[file_path], new_path)
            if key_path in PATH_KEYS:
                old_resolved = normalize_path_value(old_value, ROOT)
                new_resolved = normalize_path_value(new_value, file_path.parent)
                if old_resolved != new_resolved:
                    raise AssertionError(f"path equivalence failed for {key_path}: {old_resolved} != {new_resolved}")
                path_equal += 1
            elif new_value != expected:
                raise AssertionError(f"value equivalence failed for {key_path}: {old_value!r} -> {new_value!r}")
        equal += 1

    return {
        "source_keys": len(entries),
        "mapping_nodes": sum(entry["node_kind"] == "mapping" for entry in entries),
        "leaf_nodes": sum(entry["node_kind"] == "value" for entry in entries),
        "no_readers": sum(not entry.get("readers") for entry in entries),
        "usage": Counter(entry["usage"] for entry in entries),
        "domain": Counter(entry["domain"] for entry in entries),
        "routed_leaves": routed_leaves,
        "equal_effective_leaves": equal,
        "path_comparisons": path_equal,
        "raw_differences": raw_differences,
        "root_only_entries": sum(old_only(entry["key_path"]) for entry in entries),
    }


def build_audit(root_config: dict[str, Any], entries: list[dict[str, Any]], stats: dict[str, Any]) -> str:
    no_readers = [entry["key_path"] for entry in entries if not entry.get("readers")]
    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in ("Dataset", "Partition", "Demand", "Supply", "TTE", "Legacy / experiment")}
    for entry in entries:
        sections[route(entry["key_path"])[0]].append(entry)

    lines = [
        "# Production config split audit v1",
        "",
        "本报告是 Phase 6A 的 341-key 审计。根 `config.yaml` 继续服务旧 wrapper；split config 只由新公开单阶段入口读取。",
        "",
        "## Inventory and rules",
        "",
        f"- Source mapping keys: **{stats['source_keys']}** = {stats['mapping_nodes']} mapping/container keys + {stats['leaf_nodes']} value keys.",
        "- Structural groups: 57 dataset keys (`study_area` + `crs`), 187 stage-block keys (Partition 101, Demand 37, Supply 12, TTE 37), and 97 legacy preprocessing/baseline/reporting keys.",
        f"- Static reader evidence is absent for **{stats['no_readers']}** keys. They remain listed against root `config.yaml`; absence of a reader is not permission to drop them from the audit.",
        "- Relative paths in a stage file resolve against that stage file. `dataset_config` resolves against the stage file; dataset paths resolve against the dataset file. No current-working-directory or repository-root search is part of the new contract.",
        "- Dataset values never implicitly override stage values. `scope` is repeated only as an explicit equality gate. Root/split duplication is temporary compatibility duplication, not two competing authorities for one CLI.",
        "- YAML comments and key order are excluded from the fingerprint because the resolved mapping is stably JSON-serialized with sorted keys.",
        "- Windows drive/UNC/backslash strings must be rejected with a field-qualified error on POSIX rather than treated as relative Linux paths.",
        f"- Path-valued source leaves: **{len(PATH_KEYS)}**. The prior map classified 34; these seven additional path fields are now explicit: {', '.join(f'`{key}`' for key in sorted(MAP_MISSED_PATHS))}.",
        "- `metadata`/`notes` may be explicitly allowed by a resolver; every other unknown production-schema field must fail instead of being ignored.",
        "",
        "The `current reader` column is copied from `config-key-map-v1.json`; three entries (`crs`, root `visualization`, and `stage1_partition.regularized.visualization`) have intentionally truncated evidence there.",
        "",
        "<details><summary>30 keys with no confirmed static reader</summary>",
        "",
        *[f"- `{key}`" for key in no_readers],
        "",
        "</details>",
        "",
    ]

    header = "| Original key path | New config file | New key path | Current configured value | Current reader | New authoritative reader | Class | Formal four-stage use | Old-entry only | Path | Unit | Required | CLI override | Duplicate after split |"
    divider = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    for section, section_entries in sections.items():
        lines.extend([f"## {section}", "", header, divider])
        for entry in section_entries:
            key_path = entry["key_path"]
            _, files, new_path = route(key_path)
            value = get_path(root_config, key_path)
            files_text = "<br>".join(f"`{relative_name(path)}`" for path in files)
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{key_path}`",
                        files_text,
                        f"`{new_path}`",
                        format_default(value),
                        current_readers(entry),
                        new_reader(section, key_path),
                        category(entry, key_path),
                        formal_use(entry, key_path),
                        "是" if old_only(key_path) else "否",
                        "是" if key_path in PATH_KEYS else "否",
                        unit(entry, key_path),
                        requirement(entry, key_path),
                        cli_override(key_path),
                        duplicate_status(files),
                    ]
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_equivalence(stats: dict[str, Any]) -> str:
    legacy_count = stats["usage"]["legacy_00_05"] + stats["usage"]["legacy_baseline"]
    return f"""# Production config equivalence v1

## Result

The six committed split files parse read-only and preserve the current effective production values. The audit checked all {stats['source_keys']} source mapping keys, compared {stats['routed_leaves']} authoritative split-routed value keys, and found {stats['equal_effective_leaves']} effective-value matches. All {stats['path_comparisons']} authoritative path comparisons resolve to the same absolute targets; all {len(PATH_KEYS)} path-valued source keys remain classified in the audit, including root-only legacy/experiment paths. Repeated comparisons for `crs`/center-point values in both dataset files do not create a competing stage value.

## Split files

- `configs/datasets/fifth_ring.yaml`
- `configs/datasets/fourth_ring.yaml`
- `configs/zoning/regularized.yaml`
- `configs/pipelines/demand.yaml`
- `configs/pipelines/supply.yaml`
- `configs/pipelines/tte.yaml`

Each stage file uses `dataset_config: ../datasets/fifth_ring.yaml` and `scope: fifth_ring`. Dataset `project_root: ../..` and every other relative path are interpreted from the file containing the value, not from the process current working directory.

## Identical parameters

- Demand preserves the complete `order_pipeline` mapping: input fallbacks, SQLite/chunk size, half-open order time window, 10-minute slots, service columns, POI matching, graph weights, distance graph, and normalization.
- Supply preserves the authoritative `stage3_supply` subset: `orders_path`, `output_dir`, `max_gap_minutes=60`, `tau_idle_minutes=30`, `carpool_merge_gap_s=0`, `slot_duration_min=10`, and `n_blocks=8`.
- TTE preserves the complete root `stage4_tte` mapping: filenames, network-distance construction, inclusive time axis, trip-time band, keep-place gates, and imputation/validation values.
- Partition preserves the canonical Regularized inputs, initialization, objective, search, outputs, and all production numerical values. Input and expected-partition paths resolve to the same frozen files.

## Intentional structural changes

- `study_area.scopes.<scope>` becomes one dataset file per scope: top-level `scope`, common `crs`, `study_area` metadata, and `paths` assets.
- `schema_version: 1`, `project_root`, dataset raw/interim/processed roots, and the stage-level `dataset_config`/`scope` fields are new explicit structure; they do not replace an old numerical parameter.
- Partition adds `contract.verify_canonical: true`; the resolver reuses `stage1_partition.outputs.canonical_partition` as the expected file instead of duplicating its path.
- TTE adds optional `stage4_tte.inputs.network_distance_path` and `representative_nodes_path` standalone fallbacks. Root `config.yaml` has no corresponding keys.
- Public run ownership replaces each configured standalone output directory at execution time. The configured paths remain only fallbacks/equivalence evidence.

## Partition effective-value note

Root `config.yaml` contains `stage1_partition.regularized.search.grid.merge_split_enabled: [false, true]`, but the legacy canonical Stage 1 adapter always replaces it with `[false]` and also forces `allow_merge_split: false`. The split production config records that effective canonical value directly. This is the sole raw-text value difference and is not an algorithm or search-parameter change.

## Path changes

Path strings gained `../..` prefixes because their base moved from repository root to `configs/zoning` or `configs/pipelines`; normalized absolute paths are unchanged. Dataset paths are independently based at `configs/datasets`. Filename-only fields such as `TTE_count.parquet` remain basenames and must not be resolved as input paths or allowed to escape a stage directory.

The Phase 1 map marked 34 path leaves. This audit treats {len(PATH_KEYS)} leaves as path-valued and adds the seven previously unclassified paths listed in `production-config-split-v1.md`.

## Legacy and retained-only keys

The {legacy_count} top-level legacy preprocessing/baseline/reporting keys remain only in root `config.yaml`; they are not forced into dataset or production stage config. Partition `stage1_partition.baseline`, `regularized.evaluation`, and `regularized.visualization`, plus Supply `demand_path`, `demand_dir`, and peak-hour fields, also remain root-only because the authoritative runners do not consume them. All {stats['root_only_entries']} root-only mapping/value entries and the 30 no-reader entries remain explicit in the audit.

## CLI resource overrides

The existing formal resource key is Supply `stage3_supply.n_blocks`, exposed as `--n-blocks`. Its effective override must be written into the resolved config, config fingerprint, and manifest. Current Stage 3 has no `workers` or `chunk_size` config key, so Phase 6A must not invent aliases or silently accept arbitrary dotted-key overrides.

## Standalone fallbacks

Demand `outputs.root`, Supply `output_dir`, TTE `output_dir`, and Partition `outputs.run_root` preserve old standalone destinations as configuration evidence. A public single-stage CLI writes only to its owned run stage directory. Demand's canonical Partition input, Supply's assigned-orders input, and TTE's assigned-orders/cluster/distance inputs remain explicit stage-local fallbacks rather than implicit dataset overrides.

## Demand platform gate

The fifth-ring Demand config is runnable on Linux, but a Linux rerun is not a historical Windows order-by-order reproduction because spatial equidistance can select a different segment. Phase 6A performs tiny CLI validation only, does not publish Demand, and does not add a deterministic assignment tie-break. The full platform baseline remains Phase 9 work.

## Deferred dependencies

`python-louvain`, `python-igraph`/`leidenalg`, and `pymetis` remain deferred and are not installed by Phase 6A. Partition tiny CLI must use the canonical Regularized path or a fixed fixture that does not require those baseline packages.

## Network-distance helper debt

`roadnet_partition.graphs.distance.project_path`, `load_project_config`, and `sort_cluster_ids` remain in place. Split-config path equivalence does not authorize changing their standalone behavior. Consolidation stays deferred unless it is isolated, preserves `lib.network_distance` public names, and proves path-equivalent behavior.

## Uncertain items

The 30 keys without confirmed static reader evidence and three entries with truncated reader evidence remain explicitly recorded in the split audit. Root-only values remain preserved in `config.yaml`, not silently discarded. Root `config.yaml` remains authoritative for old wrappers throughout Phase 6A.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if committed reports are stale")
    args = parser.parse_args()

    root_config = load_yaml(ROOT_CONFIG)
    key_map = json.loads(KEY_MAP.read_text(encoding="utf-8"))
    entries = key_map["entries"]
    split_values = {path: load_yaml(path) for path in SPLIT_FILES.values()}
    split_values[ROOT_CONFIG] = root_config
    stats = validate_and_compare(root_config, entries, split_values)
    audit = build_audit(root_config, entries, stats)
    equivalence = build_equivalence(stats)

    if args.check:
        if AUDIT_REPORT.read_text(encoding="utf-8") != audit:
            raise SystemExit(f"stale report: {AUDIT_REPORT}")
        if EQUIVALENCE_REPORT.read_text(encoding="utf-8") != equivalence:
            raise SystemExit(f"stale report: {EQUIVALENCE_REPORT}")
        return
    AUDIT_REPORT.write_text(audit, encoding="utf-8")
    EQUIVALENCE_REPORT.write_text(equivalence, encoding="utf-8")


if __name__ == "__main__":
    main()
