"""Compatibility bridge for the migrated Demand implementation."""

from roadnet_partition.downstream.demand import (
    CARPOOL,
    EXCLUSIVE,
    SERVICE_TYPES,
    active_scope_name,
    aggregate_od_frame,
    build_cluster_index,
    build_cluster_od_from_staging,
    build_od_tensors,
    build_slot_labels,
    build_slot_labels_from_bounds,
    create_staging_database,
    default_relation_edges_path,
    export_assigned_orders,
    floor_datetimes_to_slot,
    format_timestamp_ns,
    infer_service_labels,
    json_safe,
    label_staged_service_types,
    load_partition,
    load_project_config,
    load_staged_slot_bounds,
    main,
    resolve_output_root,
    service_label_batches,
    sort_cluster_ids,
    stage_order_assignments,
    to_datetime_ns,
    to_epoch_ns,
)
from roadnet_partition.graphs.build import (
    build_cluster_distance_graph,
    build_cluster_poi_graph,
    build_cluster_road_edges,
    build_similarity_topk_edges,
    edges_to_adjacency,
    empty_cluster_edge_frame,
    normalize_adjacency,
    save_graph_assets,
)
from roadnet_partition.io.geospatial import (
    PROJECT_ROOT,
    display_path,
    match_points_to_segments_with_distance,
    project_path,
)

__all__ = [name for name in globals() if not name.startswith("_")]
