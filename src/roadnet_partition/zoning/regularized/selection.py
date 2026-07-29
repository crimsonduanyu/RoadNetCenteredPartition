from __future__ import annotations

from dataclasses import dataclass
import itertools
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from roadnet_partition.io.geospatial import PROJECT_ROOT

@dataclass(frozen=True)
class SearchSetting:
    lambda_c: float
    lambda_r: float
    alpha_cont: float
    alpha_conn: float
    merge_split_enabled: bool

def clean_setting_value(value: float | bool) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value).replace(".", "p").replace("-", "m")

def setting_id(setting: SearchSetting) -> str:
    if setting.alpha_cont == 1.0 and setting.alpha_conn == 1.0 and not setting.merge_split_enabled:
        return f"lc{clean_setting_value(setting.lambda_c)}_lr{clean_setting_value(setting.lambda_r)}"
    return (
        f"lc{clean_setting_value(setting.lambda_c)}"
        f"_lr{clean_setting_value(setting.lambda_r)}"
        f"_ac{clean_setting_value(setting.alpha_cont)}"
        f"_an{clean_setting_value(setting.alpha_conn)}"
        f"_ms{clean_setting_value(setting.merge_split_enabled)}"
    )

def build_settings(config: dict[str, Any]) -> list[SearchSetting]:
    grid = config["objective"]["grid"]
    lambda_c_values = [float(value) for value in grid["lambda_c"]]
    lambda_r_values = [float(value) for value in grid.get("lambda_r", [config["objective"].get("lambda_r", 1.0)])]
    alpha_cont_values = [float(value) for value in grid.get("alpha_cont", [config["objective"]["alpha_cont"]])]
    alpha_conn_values = [float(value) for value in grid.get("alpha_conn", [config["objective"]["alpha_conn"]])]
    merge_split_values = [
        bool(value)
        for value in config["search"].get("grid", {}).get(
            "merge_split_enabled",
            [bool(config["search"]["allow_merge_split"])],
        )
    ]
    return [
        SearchSetting(
            lambda_c=lambda_c,
            lambda_r=lambda_r,
            alpha_cont=alpha_cont,
            alpha_conn=alpha_conn,
            merge_split_enabled=merge_split_enabled,
        )
        for lambda_c, lambda_r, alpha_cont, alpha_conn, merge_split_enabled in itertools.product(
            lambda_c_values,
            lambda_r_values,
            alpha_cont_values,
            alpha_conn_values,
            merge_split_values,
        )
    ]

def legacy_setting_id(lambda_c: float, lambda_r: float) -> str:
    def clean(value: float) -> str:
        return str(value).replace(".", "p").replace("-", "m")

    return f"lc{clean(lambda_c)}_lr{clean(lambda_r)}"

def regularized_algorithm_name(initialization: str) -> str:
    if initialization == "demand_region_growing":
        return "regularized_region_growing"
    return f"regularized_{initialization}"

def baseline_for_algorithm(algorithm: str) -> str:
    """Inverse of regularized_algorithm_name: map a regularized algorithm name back to
    its baseline initialization name (empty string if not a regularized algorithm)."""
    if algorithm == "regularized_region_growing":
        return "demand_region_growing"
    if algorithm.startswith("regularized_"):
        return algorithm[len("regularized_") :]
    return ""

@dataclass(frozen=True)
class BestSelection:
    run_id: str
    algorithm: str
    initialization: str
    setting_id: str
    clusters_gpkg: Path
    balanced_score: float
    row: pd.Series

def project_path(path_value: str | Path) -> Path:
    if isinstance(path_value, str):
        path_value = path_value.replace("\\", "/")
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path

def require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Missing {label}: {path}")

def combined_metric_values(metrics: pd.DataFrame, name: str, regularized: pd.Series) -> pd.Series:
    if name == "order_capacity_balance":
        required = ["order_count_cv", "capacity_hinge_loss"]
        missing = [column for column in required if column not in metrics.columns]
        if missing:
            raise ValueError(f"Best-selection combined metric is missing columns: {missing}")
        parts = []
        for column in required:
            values = pd.to_numeric(metrics.loc[regularized, column], errors="coerce")
            lo = float(values.min())
            hi = float(values.max())
            if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
                normalized = pd.Series(0.0, index=metrics.index, dtype=float)
            else:
                normalized = (pd.to_numeric(metrics[column], errors="coerce") - lo) / (hi - lo)
            normalized.loc[~regularized] = np.nan
            parts.append(normalized)
        return sum(parts) / len(parts)
    if name not in metrics.columns:
        raise ValueError(f"Best-selection metric is missing from metrics_regularized.csv: {name}")
    return pd.to_numeric(metrics[name], errors="coerce")

def add_balanced_score(metrics: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    result = metrics.copy()
    weights = config["visualization"]["best_selection"]["metrics"]
    regularized = result["source_type"] == "regularized"
    for metric, weight in weights.items():
        source_values = combined_metric_values(result, metric, regularized)
        values = pd.to_numeric(source_values.loc[regularized], errors="coerce")
        lo = float(values.min())
        hi = float(values.max())
        normalized_column = f"{metric}_normalized"
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            result[normalized_column] = 0.0
        else:
            result[normalized_column] = (source_values - lo) / (hi - lo)
        result.loc[~regularized, normalized_column] = np.nan

    score = pd.Series(0.0, index=result.index, dtype=float)
    for metric, weight in weights.items():
        score += float(weight) * result[f"{metric}_normalized"].fillna(0.0)
    result["balanced_score"] = score
    result.loc[~regularized, "balanced_score"] = np.nan
    return result

def pareto_non_dominated_flags(frame: pd.DataFrame, metrics: list[str]) -> pd.Series:
    available = [metric for metric in metrics if metric in frame.columns]
    if not available:
        return pd.Series(True, index=frame.index)
    work = frame[available].apply(pd.to_numeric, errors="coerce")
    valid = work.dropna()
    flags = pd.Series(False, index=frame.index)
    for index, row in valid.iterrows():
        others = valid.drop(index=index)
        dominated = (
            (others <= row).all(axis=1)
            & (others < row).any(axis=1)
        ).any()
        flags.loc[index] = not bool(dominated)
    return flags

def select_best_run(metrics: pd.DataFrame, manifest: pd.DataFrame, config: dict[str, Any]) -> BestSelection:
    candidates = metrics.loc[metrics["source_type"] == "regularized"].copy()
    best_config = config["visualization"]["best_selection"]
    if bool(best_config.get("require_connected", True)):
        candidates = candidates.loc[pd.to_numeric(candidates["connected_cluster_ratio"], errors="coerce") >= 1.0]
    target_clusters = best_config.get("target_clusters", config.get("objective", {}).get("target_clusters"))
    if target_clusters is not None and bool(best_config.get("require_exact_target_clusters", False)):
        candidates = candidates.loc[pd.to_numeric(candidates["num_clusters"], errors="coerce") == int(target_clusters)]
    if bool(best_config.get("require_pareto_non_dominated", False)):
        pareto_metrics = best_config.get("pareto_metrics", list(best_config.get("metrics", {}).keys()))
        candidates = candidates.loc[pareto_non_dominated_flags(candidates, pareto_metrics)]
    candidates = candidates.dropna(subset=["balanced_score"]).copy()
    if candidates.empty:
        raise RuntimeError("No connected regularized candidate is available for visualization.")

    best_row = candidates.sort_values(["balanced_score", "run_id"]).iloc[0]
    manifest_match = manifest.loc[
        (manifest["algorithm"] == best_row["algorithm"])
        & (manifest["setting_id"] == best_row["setting_id"])
    ]
    if manifest_match.empty:
        raise RuntimeError(f"Unable to locate selected run in run_manifest.csv: {best_row['run_id']}")
    manifest_row = manifest_match.iloc[0]
    clusters_gpkg = project_path(manifest_row["clusters_gpkg"])
    require_file(clusters_gpkg, f"selected cluster file for {best_row['run_id']}")
    return BestSelection(
        run_id=str(best_row["run_id"]),
        algorithm=str(best_row["algorithm"]),
        initialization=str(best_row["initialization"]),
        setting_id=str(best_row["setting_id"]),
        clusters_gpkg=clusters_gpkg,
        balanced_score=float(best_row["balanced_score"]),
        row=best_row,
    )
