from __future__ import annotations

import importlib
from pathlib import Path
import sys

from roadnet_partition.zoning import evaluate, partition
from roadnet_partition.zoning.algorithms import adaptive
from roadnet_partition.zoning.regularized import objective, search, selection


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_ROOT = PROJECT_ROOT / "regularized_zoning_experiments"
if str(EXPERIMENT_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT))


def test_numbered_and_adaptive_entrypoints_are_one_way_bridges() -> None:
    numbered = importlib.import_module("03_cluster_segments")
    legacy_adaptive = importlib.import_module("adaptive_clustering")
    assert numbered.run_skater is partition.run_skater
    assert numbered.evaluate_partition is evaluate.evaluate_partition
    assert numbered.main is partition.legacy_baseline_main
    assert legacy_adaptive.run_demand_region_growing is adaptive.run_demand_region_growing


def test_regularized_experiment_entrypoints_use_new_authorities() -> None:
    runner = importlib.import_module("run_regularized_search")
    evaluator = importlib.import_module("evaluate_regularized")
    visualizer = importlib.import_module("visualize_regularized_results")
    assert runner.ObjectiveParams is objective.ObjectiveParams
    assert runner.SearchParams is search.SearchParams
    assert runner.run_from_config is partition.run_from_config
    assert evaluator.build_candidate_selection is evaluate.build_candidate_selection
    assert visualizer.add_balanced_score is selection.add_balanced_score
    assert visualizer.select_best_run is selection.select_best_run


def test_stage1_wrapper_preserves_public_helpers() -> None:
    stage1 = importlib.import_module("stages.stage1_partition")
    unified = partition.load_unified_config()
    assert stage1.main is partition.legacy_stage1_main
    assert stage1.build_regularized_config(unified, "fixture") == partition.build_regularized_config(unified, "fixture")
    assert stage1.canonical_setting_stem(unified) == partition.canonical_setting_stem(unified)
