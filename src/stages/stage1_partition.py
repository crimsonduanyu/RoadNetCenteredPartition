"""Stage 1 - spatial partitioning.

Reproduces the canonical regularized partition from the unified ``config.yaml``
by running the (deterministic) regularized search, initialized from the frozen
leiden baseline, over the inputs frozen in ``IntermediateDataForReproduce/``.

Reproduce the result:   python src/stages/stage1_partition.py
Verify against frozen:  python src/stages/stage1_partition.py --verify
Verify existing output: python src/stages/stage1_partition.py --verify-only

The regularized search and objective live in ``lib.regularized``; this script
only adapts config and orchestrates I/O. To avoid clobbering the original v2 run
under ``stage1_partition.outputs.run_root``, regenerated artifacts are written to
a sibling ``*_stage1_verify`` directory.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import geopandas as gpd  # noqa: E402
import yaml  # noqa: E402

from lib.geo import project_path  # noqa: E402
from lib.regularized import regularized_algorithm_name, run_from_config, setting_id, validate_config  # noqa: E402
from lib.regularized import SearchSetting  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def load_unified_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def build_regularized_config(unified: dict, output_root: str) -> dict:
    """Adapt the unified config's stage1 section to the regularized-search schema."""
    stage1 = unified["stage1_partition"]
    reg = stage1["regularized"]
    # The canonical partition is the merge-split-OFF setting. The official Stage 1
    # runner computes only that one setting (the full merge_split grid is an
    # experiment concern handled by run_regularized_search.py); this keeps Stage 1
    # fast and avoids the heavy, optional mson search.
    search = dict(reg["search"])
    search["allow_merge_split"] = False
    search["grid"] = {**dict(search.get("grid", {})), "merge_split_enabled": [False]}
    return {
        "scope": {
            "active": unified["study_area"]["active"],
            "graph_variant": stage1["graph_variant"],
        },
        "inputs": {**reg["inputs"], "baseline_clusters": reg["baseline_clusters"]},
        "outputs": {"root": output_root, "overwrite": True, "resume": False},
        "initializations": [reg["initialization"]],
        "objective": reg["objective"],
        "search": search,
        "evaluation": reg["evaluation"],
    }


def verify_output_root(unified: dict) -> Path:
    run_root = Path(unified["stage1_partition"]["outputs"]["run_root"])
    return run_root.parent / f"{run_root.name}_stage1_verify"


def canonical_setting_stem(unified: dict) -> str:
    """Filename stem of the canonical (merge-split-off) regularized partition."""
    stage1 = unified["stage1_partition"]
    reg = stage1["regularized"]
    obj = reg["objective"]
    algorithm = regularized_algorithm_name(reg["initialization"])
    setting = SearchSetting(
        lambda_c=float(obj["grid"]["lambda_c"][0]),
        lambda_r=float(obj["grid"].get("lambda_r", [obj["lambda_r"]])[0]),
        alpha_cont=float(obj["grid"].get("alpha_cont", [obj["alpha_cont"]])[0]),
        alpha_conn=float(obj["grid"].get("alpha_conn", [obj["alpha_conn"]])[0]),
        merge_split_enabled=False,
    )
    return f"segment_clusters_{stage1['graph_variant']}_{algorithm}_{setting_id(setting)}"


def partition_groups(gpkg_path: Path) -> set[frozenset[str]]:
    """Cluster grouping of a partition gpkg, invariant to cluster relabeling."""
    clusters = gpd.read_file(gpkg_path)
    groups: dict[object, set[str]] = {}
    for seg_id, cluster_id in zip(clusters["seg_id"].astype(str), clusters["cluster_id"]):
        groups.setdefault(cluster_id, set()).add(seg_id)
    return {frozenset(nodes) for nodes in groups.values()}


def verify_against_frozen(unified: dict, regenerated_stem: str) -> bool:
    frozen = project_path(unified["stage1_partition"]["outputs"]["canonical_partition"])
    regenerated = verify_output_root(unified) / "clusters" / f"{regenerated_stem}.gpkg"
    if not regenerated.exists():
        raise FileNotFoundError(f"Regenerated partition not found: {regenerated}")
    frozen_groups = partition_groups(frozen)
    regen_groups = partition_groups(regenerated)
    equivalent = frozen_groups == regen_groups
    print(f"frozen canonical : {frozen}  ({len(frozen_groups)} clusters)")
    print(f"regenerated      : {regenerated}  ({len(regen_groups)} clusters)")
    print(f"REPRODUCTION {'PASS: partitions are equivalent' if equivalent else 'FAIL: partitions differ'}")
    return equivalent


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    verify = "--verify" in argv
    verify_only = "--verify-only" in argv

    unified = load_unified_config()
    stem = canonical_setting_stem(unified)
    output_root = verify_output_root(unified)

    if not verify_only:
        config = build_regularized_config(unified, str(output_root))
        validate_config(config)
        print(f"Running regularized search -> {output_root}")
        run_from_config(config, CONFIG_PATH)

    if verify or verify_only:
        ok = verify_against_frozen(unified, stem)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
