from __future__ import annotations

from pathlib import Path
import sys

import networkx as nx
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lib import network_distance as nd  # noqa: E402


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
