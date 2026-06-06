"""Compatibility shim. Real code lives in ``lib.graph``.

Kept so existing ``from utils_graph import ...`` callers keep working during the
staged refactor; will be removed once all callers import ``lib.graph`` directly.
"""
from lib.graph import *  # noqa: F401,F403
