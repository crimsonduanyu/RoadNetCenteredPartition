"""Shared clustering helpers for the baseline partitioners."""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence


def allocate_component_cluster_counts(
    weights: Sequence[float],
    capacities: Sequence[int],
    target_clusters: int,
    tiebreak: Callable[[int], tuple] = lambda index: (-index,),
) -> list[int]:
    """Distribute ``target_clusters`` across connected components.

    Each component receives at least one cluster; the remaining budget is allocated
    proportional to ``weights`` (floored, capped by per-component ``capacities`` =
    component size - 1), and any leftover is handed out greedily by largest
    fractional remainder, then by the caller-supplied ``tiebreak(index)`` keys
    (highest first).

    Shared by the node-count-weighted (``src/03_cluster_segments.py``) and the
    demand-weighted (``src/adaptive_clustering.py``) baseline allocators, which pass
    their own ``weights`` and ``tiebreak`` so behavior is preserved exactly.
    """
    component_count = len(weights)
    total_nodes = sum(capacity + 1 for capacity in capacities)
    if target_clusters < component_count:
        raise ValueError(
            f"target_clusters={target_clusters} is smaller than the graph's "
            f"{component_count} connected components."
        )
    if target_clusters > total_nodes:
        raise ValueError(f"target_clusters={target_clusters} exceeds graph node count {total_nodes}.")

    allocations = [1] * component_count
    remaining = target_clusters - component_count
    if remaining == 0:
        return allocations

    total_weight = float(sum(weights))
    raw_extras = [remaining * float(weight) / total_weight for weight in weights]
    floor_extras = [min(int(math.floor(raw)), capacity) for raw, capacity in zip(raw_extras, capacities)]
    allocations = [allocation + extra for allocation, extra in zip(allocations, floor_extras)]
    remaining -= sum(floor_extras)

    while remaining > 0:
        candidates = [index for index, capacity in enumerate(capacities) if allocations[index] - 1 < capacity]
        if not candidates:
            raise ValueError("Unable to allocate requested clusters across graph components.")
        candidates.sort(
            key=lambda index: (raw_extras[index] - floor_extras[index], *tiebreak(index)),
            reverse=True,
        )
        selected = candidates[0]
        allocations[selected] += 1
        floor_extras[selected] += 1
        remaining -= 1

    return allocations
