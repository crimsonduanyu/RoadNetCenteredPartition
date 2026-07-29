from __future__ import annotations

import inspect

from lib import clustering as legacy
from roadnet_partition.zoning.algorithms import common as current


def test_legacy_clustering_export_is_compatibility_alias() -> None:
    assert legacy.__all__ == ["allocate_component_cluster_counts"]
    assert legacy.allocate_component_cluster_counts is current.allocate_component_cluster_counts


def test_clustering_signature_default_and_tiebreak_behavior_are_preserved() -> None:
    signature = inspect.signature(current.allocate_component_cluster_counts)
    assert list(signature.parameters) == ["weights", "capacities", "target_clusters", "tiebreak"]
    assert signature.parameters["tiebreak"].default(3) == (-3,)

    default_result = current.allocate_component_cluster_counts([1, 1], [2, 2], 3)
    custom_result = current.allocate_component_cluster_counts(
        [1, 1], [2, 2], 3, tiebreak=lambda index: (index,)
    )
    assert default_result == [2, 1]
    assert custom_result == [1, 2]
