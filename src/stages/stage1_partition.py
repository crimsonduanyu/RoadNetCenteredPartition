"""Compatibility entrypoint for Stage 1 spatial partitioning."""

from pathlib import Path

import geopandas as gpd

from roadnet_partition.zoning.partition import (
    CONFIG_PATH,
    build_regularized_config,
    canonical_setting_stem,
    legacy_stage1_main as main,
    load_unified_config,
    verify_against_frozen,
    verify_output_root,
)
from roadnet_partition.zoning.contracts import partition_groups as _partition_groups


def partition_groups(gpkg_path: Path) -> set[frozenset[str]]:
    return _partition_groups(gpd.read_file(gpkg_path))

__all__ = [name for name in globals() if not name.startswith("_")]


if __name__ == "__main__":
    main()
