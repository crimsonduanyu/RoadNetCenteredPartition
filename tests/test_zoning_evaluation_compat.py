from __future__ import annotations

import importlib
from pathlib import Path
import sys

import pandas as pd
import pandas.testing as pdt

from roadnet_partition.zoning import evaluate
from roadnet_partition.zoning.regularized import selection


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_ROOT = PROJECT_ROOT / "regularized_zoning_experiments"
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))


def test_pareto_and_candidate_selection_match_legacy() -> None:
    legacy = importlib.import_module("evaluate_regularized")
    metrics = pd.DataFrame(
        [
            {"source_type": "baseline", "graph_variant": "g", "algorithm": "louvain", "run_id": "base", "x": 2.0, "y": 2.0, "continuity_edge_cut_ratio": .2, "connector_edge_cut_ratio": .2, "od_sparsity": .5, "order_count_cv": .5, "capacity_violation_ratio": .3, "mean_elongation": 2.0, "mean_network_diameter_m": 3.0},
            {"source_type": "regularized", "graph_variant": "g", "algorithm": "regularized_louvain", "run_id": "a", "x": 1.0, "y": 3.0, "continuity_edge_cut_ratio": .19, "connector_edge_cut_ratio": .19, "od_sparsity": .5, "order_count_cv": .4, "capacity_violation_ratio": .2, "mean_elongation": 2.0, "mean_network_diameter_m": 3.0},
            {"source_type": "regularized", "graph_variant": "g", "algorithm": "regularized_louvain", "run_id": "b", "x": 3.0, "y": 1.0, "continuity_edge_cut_ratio": .21, "connector_edge_cut_ratio": .21, "od_sparsity": .5, "order_count_cv": .6, "capacity_violation_ratio": .4, "mean_elongation": 2.0, "mean_network_diameter_m": 3.0},
        ]
    )
    config = {"evaluation": {"pareto_pairs": [{"name": "xy", "x": "x", "y": "y"}]}}
    pdt.assert_frame_equal(evaluate.build_pareto_summary(metrics, config), legacy.build_pareto_summary(metrics, config))
    pdt.assert_frame_equal(evaluate.build_candidate_selection(metrics, config), legacy.build_candidate_selection(metrics, config))


def test_balanced_score_pareto_and_tie_break_match_legacy(tmp_path: Path) -> None:
    legacy = importlib.import_module("visualize_regularized_results")
    metrics = pd.DataFrame(
        [
            {"source_type": "regularized", "run_id": "b", "algorithm": "regularized_louvain", "initialization": "louvain", "setting_id": "s", "connected_cluster_ratio": 1.0, "num_clusters": 2, "m": 1.0},
            {"source_type": "regularized", "run_id": "a", "algorithm": "regularized_louvain", "initialization": "louvain", "setting_id": "s", "connected_cluster_ratio": 1.0, "num_clusters": 2, "m": 1.0},
        ]
    )
    config = {"objective": {"target_clusters": 2}, "visualization": {"best_selection": {"metrics": {"m": 1.0}, "require_connected": True}}}
    old_scored = legacy.add_balanced_score(metrics, config)
    new_scored = selection.add_balanced_score(metrics, config)
    pdt.assert_frame_equal(new_scored, old_scored)
    assert selection.pareto_non_dominated_flags(new_scored, ["m"]).equals(legacy.pareto_non_dominated_flags(old_scored, ["m"]))
    cluster_file = tmp_path / "clusters.gpkg"
    cluster_file.touch()
    manifest = pd.DataFrame([{"algorithm": "regularized_louvain", "setting_id": "s", "clusters_gpkg": str(cluster_file)}])
    old_best = legacy.select_best_run(old_scored, manifest, config)
    new_best = selection.select_best_run(new_scored, manifest, config)
    assert new_best.run_id == old_best.run_id == "a"
    assert new_best.balanced_score == old_best.balanced_score
