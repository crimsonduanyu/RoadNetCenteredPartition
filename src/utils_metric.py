"""Compatibility shim. Real code lives in ``lib.metrics``.

Kept so existing ``from utils_metric import ...`` callers keep working during the
staged refactor; will be removed once all callers import ``lib.metrics`` directly.
"""
from lib.metrics import *  # noqa: F401,F403
