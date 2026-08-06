from __future__ import annotations

import json
import math
import os
import random
import subprocess
import sys

import networkx as nx
import numpy as np
import pandas as pd
import pytest

from roadnet_partition.zoning.evaluate import build_candidate_selection
from roadnet_partition.zoning.metrics import (
    EPS,
    NETWORK_DIAMETER_ALGORITHM,
    NETWORK_DIAMETER_METRIC_VERSION,
    NETWORK_DIAMETER_WEIGHT_SEMANTICS,
    MetricThresholds,
    exact_network_diameter,
    network_diameter_metrics,
)


COUNTEREXAMPLE_EDGES = [("0", "1"), ("0", "2"), ("0", "3"), ("1", "2"), ("1", "3")]


def ground_truth(graph: nx.Graph, lengths: dict) -> float:
    def weight(u, v, attrs):
        return max((float(lengths.get(u, 0.0)) + float(lengths.get(v, 0.0))) / 2.0, EPS)

    return max(
        (max(distances.values()) for _, distances in nx.all_pairs_dijkstra_path_length(graph, weight=weight)),
        default=0.0,
    )


def metric(graph: nx.Graph, lengths: dict, partition: dict | None = None) -> dict:
    nodes = list(lengths)
    clusters = pd.DataFrame({"seg_id": nodes, "cluster_id": [0] * len(nodes), "length": list(lengths.values())})
    return network_diameter_metrics(
        graph,
        clusters,
        partition or {node: 0 for node in nodes},
        MetricThresholds(large_diameter_threshold_m=2.5),
    )


@pytest.mark.parametrize(
    ("graph", "lengths", "expected"),
    [
        (nx.Graph(), {}, 0.0),
        (nx.empty_graph(["a"]), {"a": 1.0}, 0.0),
        (nx.path_graph(["a", "b"]), {"a": 1.0, "b": 1.0}, 1.0),
        (nx.path_graph(5), {node: 1.0 for node in range(5)}, 4.0),
        (nx.star_graph(4), {node: 1.0 for node in range(5)}, 2.0),
        (nx.cycle_graph(6), {node: 1.0 for node in range(6)}, 3.0),
        (nx.path_graph(3), {0: 1.0, 1: 3.0, 2: 5.0}, 6.0),
    ],
)
def test_exact_basic_semantics(graph: nx.Graph, lengths: dict, expected: float) -> None:
    assert exact_network_diameter(graph, lengths) == pytest.approx(expected, abs=1e-12)


def test_disconnected_components_and_isolates_use_finite_component_maximum() -> None:
    graph = nx.Graph()
    graph.add_edges_from([("a", "b"), ("b", "c"), ("x", "y")])
    graph.add_node("isolated")
    lengths = {node: 1.0 for node in graph}
    assert exact_network_diameter(graph, lengths) == 2.0


def test_aud004_cyclic_counterexample_is_exact_for_all_insertion_orders() -> None:
    lengths = {str(node): 1.0 for node in range(4)}
    orders = [COUNTEREXAMPLE_EDGES, list(reversed(COUNTEREXAMPLE_EDGES)), COUNTEREXAMPLE_EDGES[2:] + COUNTEREXAMPLE_EDGES[:2]]
    for edges in orders:
        graph = nx.Graph()
        graph.add_nodes_from(reversed(list(lengths)))
        graph.add_edges_from(edges)
        assert exact_network_diameter(graph, lengths) == 2.0
        result = metric(graph, lengths)
        assert result["mean_network_diameter_m"] == 2.0
        assert result["max_network_diameter_m"] == 2.0
        reversed_partition = dict(reversed([(node, 0) for node in lengths]))
        assert metric(graph, lengths, reversed_partition) == result


@pytest.mark.parametrize(
    ("edges", "lengths", "expected"),
    [
        ([(0, 2), (0, 3), (1, 2), (1, 3)], {0: 0.25, 1: 0.25, 2: 3.0, 3: 5.0}, 4.25),
        ([(0, 1), (0, 2), (0, 3), (1, 2), (1, 3)], {0: 1.0, 1: 1.0, 2: 0.5, 3: 1.0}, 1.75),
    ],
)
def test_weighted_cyclic_counterexamples_match_networkx_ground_truth(edges, lengths, expected) -> None:
    graph = nx.Graph(edges)
    assert exact_network_diameter(graph, lengths) == pytest.approx(expected, abs=1e-12)
    assert exact_network_diameter(graph, lengths) == pytest.approx(ground_truth(graph, lengths), abs=1e-12)


def test_fixed_random_graphs_match_all_pairs_ground_truth() -> None:
    rng = random.Random(20260804)
    graphs = [nx.random_labeled_tree(12, seed=1)]
    for seed in range(4):
        graph = nx.gnp_random_graph(14, 0.24, seed=seed)
        graph.add_edges_from((node, node + 1) for node in range(13))
        graphs.append(graph)
    graphs.append(nx.disjoint_union(nx.path_graph(7), nx.cycle_graph(9)))
    for graph in graphs:
        lengths = {node: rng.choice([0.0, EPS, 0.25, 1.0, 3.5]) for node in graph}
        assert exact_network_diameter(graph, lengths) == pytest.approx(
            ground_truth(graph, lengths), rel=1e-14, abs=1e-12
        )


def test_weight_eps_and_missing_node_fallback_contract() -> None:
    edge = nx.path_graph(["a", "b"])
    assert exact_network_diameter(edge, {"a": 0.0, "b": 0.0}) == EPS
    assert exact_network_diameter(edge, {"a": EPS, "b": EPS}) == EPS
    assert exact_network_diameter(edge, {"a": 2.0}) == 1.0
    assert exact_network_diameter(edge, {}) == EPS


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), float("-inf"), "bad"])
def test_invalid_lengths_are_rejected(value) -> None:
    with pytest.raises(ValueError, match="length"):
        exact_network_diameter(nx.path_graph(["a", "b"]), {"a": value, "b": 1.0})


def test_metric_rejects_missing_duplicate_and_nonfinite_length_records() -> None:
    graph = nx.path_graph(["a", "b"])
    with pytest.raises(ValueError, match="missing required columns"):
        network_diameter_metrics(graph, pd.DataFrame({"seg_id": ["a", "b"]}), {"a": 0, "b": 0}, MetricThresholds())
    with pytest.raises(ValueError, match="duplicate seg_id"):
        network_diameter_metrics(
            graph,
            pd.DataFrame({"seg_id": ["a", "a"], "length": [1.0, 1.0]}),
            {"a": 0},
            MetricThresholds(),
        )
    with pytest.raises(ValueError, match="finite and non-negative"):
        network_diameter_metrics(
            graph,
            pd.DataFrame({"seg_id": ["a", "b"], "length": [1.0, np.nan]}),
            {"a": 0, "b": 0},
            MetricThresholds(),
        )


def test_graph_type_and_node_identifier_contract() -> None:
    multigraph = nx.MultiGraph()
    multigraph.add_edges_from([("北京", 1), ("北京", 1), (1, "1")])
    lengths = {"北京": 1.0, 1: 1.0, "1": 1.0}
    assert exact_network_diameter(multigraph, lengths) == 2.0
    with pytest.raises(ValueError, match="undirected"):
        exact_network_diameter(nx.DiGraph([("a", "b")]), {"a": 1.0, "b": 1.0})


def selection_flag(baseline_diameter: float, candidate_diameter: float) -> bool:
    common = {
        "continuity_edge_cut_ratio": 1.0,
        "connector_edge_cut_ratio": 1.0,
        "od_sparsity": 1.0,
        "order_count_cv": 1.0,
        "capacity_violation_ratio": 1.0,
        "mean_elongation": 1.0,
    }
    metrics = pd.DataFrame([
        {**common, "source_type": "baseline", "run_id": "louvain", "algorithm": "louvain", "mean_network_diameter_m": baseline_diameter},
        {**common, "source_type": "regularized", "run_id": "candidate", "algorithm": "regularized_louvain", "mean_network_diameter_m": candidate_diameter},
    ])
    config = {"evaluation": {"selection_tolerances": {"shape_max_relative_degradation": 0.10}}}
    return bool(build_candidate_selection(metrics, config).iloc[0]["diameter_not_worse"])


def test_exact_evaluation_decision_corrects_candidate_and_baseline_underestimates() -> None:
    graph = nx.Graph(COUNTEREXAMPLE_EDGES)
    lengths = {str(node): 1.0 for node in range(4)}
    exact = metric(graph, lengths)["mean_network_diameter_m"]
    assert exact == 2.0
    assert selection_flag(1.5, 1.0) is True   # legacy candidate underestimate
    assert selection_flag(1.5, exact) is False
    assert selection_flag(1.0, 1.5) is False  # legacy baseline underestimate
    assert selection_flag(exact, 1.5) is True


def test_hash_seed_production_metric_and_evaluation_serialization_are_stable() -> None:
    code = r'''
import json, networkx as nx, pandas as pd
from roadnet_partition.zoning.metrics import MetricThresholds, network_diameter_metrics
from roadnet_partition.zoning.evaluate import build_candidate_selection
g=nx.Graph(); g.add_edges_from([("0","1"),("0","2"),("0","3"),("1","2"),("1","3")])
c=pd.DataFrame({"seg_id":["0","1","2","3"],"cluster_id":[0,0,0,0],"length":[1.,1.,1.,1.]})
d=network_diameter_metrics(g,c,{str(i):0 for i in range(4)},MetricThresholds())["mean_network_diameter_m"]
common={"continuity_edge_cut_ratio":1.,"connector_edge_cut_ratio":1.,"od_sparsity":1.,"order_count_cv":1.,"capacity_violation_ratio":1.,"mean_elongation":1.}
rows=pd.DataFrame([{**common,"source_type":"baseline","run_id":"louvain","algorithm":"louvain","mean_network_diameter_m":1.5},{**common,"source_type":"regularized","run_id":"candidate","algorithm":"regularized_louvain","mean_network_diameter_m":d}])
selection=build_candidate_selection(rows,{"evaluation":{"selection_tolerances":{"shape_max_relative_degradation":.1}}})
print(json.dumps({"diameter":d,"selection":json.loads(selection.to_json(orient="records"))},sort_keys=True))
'''
    outputs = []
    for seed in (0, 1, 2, 3, 7, 11, 42, 99):
        environment = {**os.environ, "PYTHONHASHSEED": str(seed)}
        outputs.append(subprocess.check_output([sys.executable, "-c", code], env=environment, text=True).strip())
    assert len(set(outputs)) == 1
    payload = json.loads(outputs[0])
    assert payload["diameter"] == 2.0
    assert payload["selection"][0]["diameter_not_worse"] is False


def test_metric_metadata_and_empty_partition_contract() -> None:
    result = network_diameter_metrics(
        nx.Graph(),
        pd.DataFrame({"seg_id": pd.Series(dtype=str), "length": pd.Series(dtype=float)}),
        {},
        MetricThresholds(),
    )
    assert math.isnan(result["mean_network_diameter_m"])
    assert result["network_diameter_metric_version"] == NETWORK_DIAMETER_METRIC_VERSION == 2
    assert result["network_diameter_algorithm"] == NETWORK_DIAMETER_ALGORITHM
    assert result["network_diameter_weight_semantics"] == NETWORK_DIAMETER_WEIGHT_SEMANTICS
