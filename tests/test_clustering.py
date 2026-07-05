from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lib.clustering import allocate_component_cluster_counts


def _size_weighted(sizes: list[int], target: int) -> list[int]:
    return allocate_component_cluster_counts(
        weights=[float(s) for s in sizes],
        capacities=[s - 1 for s in sizes],
        target_clusters=target,
        tiebreak=lambda index: (sizes[index], -index),
    )


def test_allocate_proportional_with_fractional_remainder() -> None:
    # sizes [10,20,30], 9 extra -> raw [1.5,3.0,4.5] -> floor [1,3,4], 1 left ->
    # largest fractional remainder ties at 0.5 (idx 0 and 2); size tiebreak picks idx 2.
    assert _size_weighted([10, 20, 30], 12) == [2, 4, 6]


def test_allocate_invariants() -> None:
    sizes, target = [3, 5, 100], 50
    out = _size_weighted(sizes, target)
    assert sum(out) == target
    assert all(1 <= count <= size for count, size in zip(out, sizes))


def test_allocate_one_per_component_when_target_equals_components() -> None:
    assert _size_weighted([4, 9, 2], 3) == [1, 1, 1]


def test_allocate_demand_weighted_tiebreak_differs_from_size() -> None:
    # Equal sizes, unequal demand -> the extra cluster follows demand weight.
    sizes = [10, 10]
    demands = [1.0, 9.0]
    out = allocate_component_cluster_counts(
        weights=demands,
        capacities=[s - 1 for s in sizes],
        target_clusters=3,
        tiebreak=lambda index: (demands[index], sizes[index], -index),
    )
    assert out == [1, 2]


def test_allocate_validates_bounds() -> None:
    with pytest.raises(ValueError):
        _size_weighted([5, 5], 1)  # target < number of components
    with pytest.raises(ValueError):
        _size_weighted([2, 3], 99)  # target > total node count
