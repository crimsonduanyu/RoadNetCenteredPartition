from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np


SCRIPT = Path(__file__).parents[1] / "scripts/analysis/diagnose_demand_spatial_differences.py"
SPEC = importlib.util.spec_from_file_location("demand_spatial_diagnostics", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_distance_classes_use_declared_tolerance() -> None:
    result = MODULE.distance_classes(
        np.array([1.0, 1.0, 1.0, 1.0]),
        np.array([1.0, 1.0 + 5e-7, 2.0, 0.5]),
        absolute_tolerance_m=1e-6,
        relative_tolerance=1e-12,
    )

    assert result["exact"].tolist() == [True, False, False, False]
    assert result["approximate"].tolist() == [True, True, False, False]
    assert result["old_clearly_closer"].tolist() == [False, False, True, False]
    assert result["new_clearly_closer"].tolist() == [False, False, False, True]
