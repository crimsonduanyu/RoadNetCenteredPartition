"""Compatibility shim. Real code lives in ``lib.geo``.

Kept so existing ``from utils_geo import ...`` callers keep working during the
staged refactor; will be removed once all callers import ``lib.geo`` directly.
"""
from lib.geo import *  # noqa: F401,F403
