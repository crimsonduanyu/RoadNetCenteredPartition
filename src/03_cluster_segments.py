"""Compatibility entrypoint for baseline map partitioning."""

from roadnet_partition.zoning.algorithms.leiden import run_leiden
from roadnet_partition.zoning.algorithms.louvain import run_louvain
from roadnet_partition.zoning.algorithms.metis import metis_vertex_weights, run_metis
from roadnet_partition.zoning.algorithms.network_voronoi import run_demand_network_voronoi
from roadnet_partition.zoning.algorithms.region_growing import run_demand_region_growing
from roadnet_partition.zoning.algorithms.skater import (
    allocate_component_cluster_counts,
    configured_target_clusters,
    edge_dissimilarity,
    run_skater,
)
from roadnet_partition.zoning.contracts import save_baseline_partition_outputs as save_partition_outputs
from roadnet_partition.zoning.evaluate import (
    build_cluster_summary,
    build_ranked_summary,
    build_road_name_diagnostics,
    cluster_connectivity_ratio,
    cluster_od_sparsity,
    dominant_value,
    edge_cut_ratio,
    evaluate_partition,
    intra_edge_similarity,
)
from roadnet_partition.zoning.partition import ALGORITHM_RUNNERS, legacy_baseline_main as main

__all__ = [name for name in globals() if not name.startswith("_")]


if __name__ == "__main__":
    main()
