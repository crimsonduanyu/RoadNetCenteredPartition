from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from roadnet_partition.io.paths import resolve_path


@dataclass(frozen=True)
class ResolvedStageConfig:
    source_path: Path
    values: Mapping[str, Any]
    fingerprint: str


def stable_value(value: Any) -> Any:
    """Convert configuration/runtime values into stable JSON-compatible data."""
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): stable_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [stable_value(item) for item in value]
    if isinstance(value, set):
        items = [stable_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported value for stable serialization: {type(value).__name__}")


def config_fingerprint(values: Mapping[str, Any]) -> str:
    payload = json.dumps(
        stable_value(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _resolve_field(values: dict[str, Any], field: str, config_dir: Path) -> None:
    parts = field.split(".")
    current: Any = values
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if not isinstance(current, dict) or parts[-1] not in current:
        return
    value = current[parts[-1]]
    if value is None:
        return
    if isinstance(value, list):
        current[parts[-1]] = [resolve_path(item, base_dir=config_dir) for item in value]
    elif isinstance(value, (str, Path)):
        current[parts[-1]] = resolve_path(value, base_dir=config_dir)
    else:
        raise TypeError(f"declared path field {field!r} must contain a path or list of paths")


def load_stage_config(
    source_path: str | Path,
    *,
    path_fields: tuple[str, ...] = (),
) -> ResolvedStageConfig:
    source = Path(source_path).expanduser().resolve()
    with source.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ValueError("configuration root must be a YAML mapping")

    values = dict(loaded)
    if "project_root" in values and values["project_root"] is not None:
        values["project_root"] = resolve_path(values["project_root"], base_dir=source.parent)
    for field in path_fields:
        _resolve_field(values, field, source.parent)
    return ResolvedStageConfig(source, values, config_fingerprint(values))
