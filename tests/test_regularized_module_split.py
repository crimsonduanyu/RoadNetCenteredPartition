from __future__ import annotations

import ast
from pathlib import Path

from roadnet_partition.zoning import partition
from roadnet_partition.zoning.regularized import objective, search, selection


EXPECTED_DEFINITIONS = {
    "objective.py": {
        "ObjectiveParams", "SearchContext", "edge_value", "build_context",
        "target_cluster_count", "target_demand", "build_cluster_nodes",
        "build_cluster_demand", "capacity_penalty_for_value",
        "capacity_component", "objective_components", "move_delta",
    },
    "search.py": {
        "SearchParams", "MacroCandidate", "relabel_partition",
        "source_remains_connected", "assert_partition_valid",
        "assert_exact_target_clusters", "cluster_adjacency",
        "ranked_merge_sources", "ranked_split_sources",
        "candidate_merge_targets", "apply_merge", "split_seed_pair",
        "split_assignment", "apply_split", "build_macro_candidate",
        "best_merge_only_candidate", "best_split_only_candidate",
        "greedy_merge_once_to_reduce_k", "normalize_partition_to_target",
        "best_merge_split_candidate", "run_search",
    },
    "selection.py": {
        "SearchSetting", "clean_setting_value", "setting_id",
        "build_settings", "legacy_setting_id", "regularized_algorithm_name",
        "baseline_for_algorithm", "BestSelection", "project_path",
        "require_file", "combined_metric_values", "add_balanced_score",
        "pareto_non_dominated_flags", "select_best_run",
    },
    "partition.py": {
        "load_config", "require_keys", "validate_config", "load_graph",
        "load_demand", "load_partition",
        "write_run_config", "append_rows", "run_from_config", "run_partition",
        "run_baseline_partition", "_run_baseline_partition",
        "legacy_baseline_main", "load_unified_config",
        "build_regularized_config", "verify_output_root",
        "canonical_setting_stem", "verify_against_frozen",
        "legacy_stage1_main",
    },
}


def test_mechanical_split_has_every_definition_once() -> None:
    modules = [objective, search, selection, partition]
    found = {}
    for module in modules:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        names = {
            node.name for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        }
        found[Path(module.__file__).name] = names
    assert found == EXPECTED_DEFINITIONS
    all_names = [name for names in found.values() for name in names]
    assert len(all_names) == len(set(all_names))


def test_regularized_modules_have_no_import_cycle() -> None:
    assert objective.SearchContext.__module__ == objective.__name__
    assert search.SearchParams.__module__ == search.__name__
    assert selection.SearchSetting.__module__ == selection.__name__
