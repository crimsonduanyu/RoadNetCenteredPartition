"""Low-invasive, disableable stage timing for the Demand stage.

Gated by the ``ROADNET_DEMAND_TIMING`` environment variable (off by default, so
production pays nothing and no output changes). When enabled, it records only
aggregate wall-clock durations per phase (CSV parse, point construction, spatial-
index build, nearest query, SQLite append, service labeling, gzip write, OD
aggregation); it never records order-level information. The summary is printed
to stderr at the end of the stage so it cannot perturb persisted outputs.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Iterator

_ENV_VAR = "ROADNET_DEMAND_TIMING"
_TRUTHY = {"1", "true", "yes", "on"}


class StageTimer:
    """Accumulates wall-clock durations per named phase. A no-op when disabled."""

    def __init__(self, name: str, enabled: bool) -> None:
        self.name = name
        self.enabled = enabled
        self._phases: dict[str, float] = {}

    @contextlib.contextmanager
    def phase(self, label: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        yield
        self._phases[label] = self._phases.get(label, 0.0) + (time.perf_counter() - start)

    def report(self) -> None:
        if not self.enabled or not self._phases:
            return
        print(f"[stage-timing:{self.name}]", file=sys.stderr)
        for label, seconds in sorted(self._phases.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {label}: {seconds:.3f}s", file=sys.stderr)


_ACTIVE: StageTimer | None = None


def _is_enabled() -> bool:
    return os.environ.get(_ENV_VAR, "").strip().lower() in _TRUTHY


def reset(name: str = "demand") -> StageTimer:
    """Start a fresh timer for a stage (called at the start of the stage run)."""
    global _ACTIVE
    _ACTIVE = StageTimer(name, _is_enabled())
    return _ACTIVE


def get_active_timer() -> StageTimer:
    """Return the active timer (creating a disabled one if none is set)."""
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = StageTimer("demand", _is_enabled())
    return _ACTIVE
