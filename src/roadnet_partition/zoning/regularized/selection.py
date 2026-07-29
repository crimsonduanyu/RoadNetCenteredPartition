from __future__ import annotations

from dataclasses import dataclass
import itertools
from typing import Any

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
