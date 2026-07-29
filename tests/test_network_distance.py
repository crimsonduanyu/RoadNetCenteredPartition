from __future__ import annotations

import networkx as nx
import numpy as np
import pytest

from lib import network_distance as legacy
from roadnet_partition.graphs import distance as nd


PUBLIC_NAMES = {
    "project_path", "load_project_config", "sort_cluster_ids", "collapse_min_undirected",
    "load_osm_graph_undirected_min", "build_filtered_subgraph", "cluster_osm_nodes",
    "project_node_coords", "pick_representatives", "compute_distance_matrix", "build_or_load",
}


def test_legacy_network_distance_exports_are_compatibility_aliases() -> None:
    assert set(legacy.__all__) == PUBLIC_NAMES
    for name in PUBLIC_NAMES:
        assert getattr(legacy, name) is getattr(nd, name)


# --------------------------------------------------------------------------
# collapse_min_undirected: parallel edges / both directions take the minimum
# --------------------------------------------------------------------------
def test_collapse_min_undirected_takes_min_length() -> None:
    mg = nx.MultiDiGraph()
    mg.add_edge(1, 2, length=10.0)
    mg.add_edge(1, 2, length=4.0)   # parallel, shorter
    mg.add_edge(2, 1, length=7.0)   # reverse direction
    mg.add_edge(2, 3, length=5.0)
    mg.add_edge(9, 9, length=1.0)   # self-loop, dropped
    g = nd.collapse_min_undirected(mg)
    assert not g.is_directed()
    assert g[1][2]["length"] == 4.0   # min over parallel + reverse
    assert g[2][3]["length"] == 5.0
    assert not g.has_edge(9, 9)
    assert g.number_of_edges() == 2


# --------------------------------------------------------------------------
# pick_representatives: nearest to centroid, ties -> smallest osmid
# --------------------------------------------------------------------------
def test_pick_representatives_nearest_and_tiebreak() -> None:
    cluster_nodes = {"0": {10, 11, 12}, "1": {20, 21}}
    node_xy = {10: (0.0, 0.0), 11: (1.0, 0.0), 12: (5.0, 0.0), 20: (10.0, 0.0), 21: (10.0, 2.0)}
    centroids = {"0": (0.4, 0.0), "1": (10.0, 0.0)}
    reps = nd.pick_representatives(cluster_nodes, node_xy, centroids)
    assert reps["0"]["rep"] == 10           # nearest to (0.4,0)
    assert reps["1"]["rep"] == 20           # exactly on node 20

    # Tie between 10 and 11 (centroid at 0.5) -> smaller osmid 10
    tie = nd.pick_representatives({"0": {10, 11}}, {10: (0.0, 0.0), 11: (1.0, 0.0)}, {"0": (0.5, 0.0)})
    assert tie["0"]["rep"] == 10


# --------------------------------------------------------------------------
# compute_distance_matrix: filtered-first, raw fallback, symmetric, diag 0
# --------------------------------------------------------------------------
def test_distance_matrix_filtered_with_raw_fallback() -> None:
    # Filtered layer: two disconnected components {1-2} and {3-4}.
    g_filt = nx.Graph()
    g_filt.add_edge(1, 2, length=100.0)
    g_filt.add_edge(3, 4, length=100.0)
    # Raw layer: a bridge 2-3 makes everything connected.
    g_raw = nx.Graph()
    g_raw.add_edge(1, 2, length=100.0)
    g_raw.add_edge(3, 4, length=100.0)
    g_raw.add_edge(2, 3, length=50.0)

    reps = {"0": 1, "1": 3}
    D = nd.compute_distance_matrix(g_filt, g_raw, reps, ["0", "1"])

    assert D.loc["0", "0"] == 0.0 and D.loc["1", "1"] == 0.0      # zero diagonal
    # 0->1 unreachable in filtered -> raw fallback path 1-2-3 = 150
    assert D.loc["0", "1"] == 150.0
    assert D.loc["1", "0"] == 150.0                               # symmetric
    arr = D.to_numpy()
    assert np.isfinite(arr).all()                                # no inf after fallback
    assert np.allclose(arr, arr.T)


def test_distance_matrix_filtered_path_preferred() -> None:
    # Filtered layer already connects 1 and 3 via 1-2-3 (total 30);
    # raw has a shortcut 1-3 (length 5) that must NOT be used (filtered first).
    g_filt = nx.Graph()
    g_filt.add_edge(1, 2, length=10.0)
    g_filt.add_edge(2, 3, length=20.0)
    g_raw = nx.Graph()
    g_raw.add_edge(1, 2, length=10.0)
    g_raw.add_edge(2, 3, length=20.0)
    g_raw.add_edge(1, 3, length=5.0)
    D = nd.compute_distance_matrix(g_filt, g_raw, {"0": 1, "1": 3}, ["0", "1"])
    assert D.loc["0", "1"] == 30.0  # filtered distance, raw shortcut ignored


def test_distance_matrix_preserves_requested_nonnumeric_cluster_order() -> None:
    graph = nx.Graph()
    graph.add_edge(1, 2, length=1.0)
    graph.add_edge(2, 3, length=1.0)
    order = ["10", "2", "A"]
    matrix = nd.compute_distance_matrix(graph, graph, {"10": 1, "2": 2, "A": 3}, order)
    assert list(matrix.index) == order
    assert list(matrix.columns) == order
    assert matrix.loc["10", "A"] == 2.0


def test_distance_matrix_isolated_node_and_unreachable_semantics() -> None:
    graph = nx.Graph()
    graph.add_edge(1, 2, length=2.0)
    graph.add_node(9)
    matrix = nd.compute_distance_matrix(graph, graph, {"a": 1, "isolated": 9}, ["isolated", "a"])
    assert matrix.loc["isolated", "isolated"] == 0.0
    assert np.isinf(matrix.loc["isolated", "a"])
    assert np.isinf(matrix.loc["a", "isolated"])


def test_equal_paths_and_missing_weight_keep_networkx_behavior() -> None:
    equal = nx.Graph()
    equal.add_edge(1, 2, length=1.0)
    equal.add_edge(2, 4, length=1.0)
    equal.add_edge(1, 3, length=1.0)
    equal.add_edge(3, 4, length=1.0)
    matrix = nd.compute_distance_matrix(equal, equal, {"x": 1, "y": 4}, ["x", "y"])
    assert matrix.loc["x", "y"] == 2.0

    missing_weight = nx.Graph()
    missing_weight.add_edge(1, 2)
    defaulted = nd.compute_distance_matrix(missing_weight, missing_weight, {"x": 1, "y": 2}, ["x", "y"])
    assert defaulted.loc["x", "y"] == 1.0

    absent_rep = nx.Graph()
    absent_rep.add_node(1)
    with pytest.raises(nx.NodeNotFound):
        nd.compute_distance_matrix(absent_rep, absent_rep, {"x": 1, "missing": 2}, ["x", "missing"])
