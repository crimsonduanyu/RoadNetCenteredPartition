"""Compatibility bridge for adaptive zoning algorithms."""

from roadnet_partition.zoning.algorithms.adaptive import (
    EPS,
    AdaptiveContext,
    AdaptiveParams,
    adaptive_params,
    allocate_component_cluster_counts,
    assign_remaining_by_nearest_cluster,
    build_cluster_nodes,
    capacitated_multisource_assign,
    demand_signal,
    edge_cost,
    grow_component_regions,
    improve_boundaries,
    load_context,
    load_segment_centroids,
    load_segment_demand,
    move_objective_delta,
    multisource_assign,
    region_candidate_score,
    relabel_partition,
    run_demand_network_voronoi,
    run_demand_region_growing,
    select_demand_weighted_seeds,
    source_remains_connected,
    update_centroid,
)

__all__ = [name for name in globals() if not name.startswith("_")]
