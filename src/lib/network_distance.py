"""Compatibility bridge for :mod:`roadnet_partition.graphs.distance`."""

from roadnet_partition.graphs.distance import (
    build_filtered_subgraph,
    build_or_load,
    cluster_osm_nodes,
    collapse_min_undirected,
    compute_distance_matrix,
    load_osm_graph_undirected_min,
    load_project_config,
    pick_representatives,
    project_node_coords,
    project_path,
    sort_cluster_ids,
)

__all__ = [
    "project_path",
    "load_project_config",
    "sort_cluster_ids",
    "collapse_min_undirected",
    "load_osm_graph_undirected_min",
    "build_filtered_subgraph",
    "cluster_osm_nodes",
    "project_node_coords",
    "pick_representatives",
    "compute_distance_matrix",
    "build_or_load",
]
