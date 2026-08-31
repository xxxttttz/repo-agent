"""Small YAML configuration loader with nested command-line overrides."""

import json
from pathlib import Path
from typing import Any

import yaml

builtin_config_dir = Path(__file__).parent


def _key_value_spec_to_nested_dict(spec: str) -> dict:
    key, value = spec.split("=", 1)
    keys = key.split(".")
    if not key or any(not part for part in keys):
        raise ValueError(f"Invalid config spec {spec!r}: empty config key")
    try:
        value = json.loads(value)
    except json.JSONDecodeError:
        pass
    result: dict[str, Any] = {}
    current = result
    for part in keys[:-1]:
        current[part] = {}
        current = current[part]
    current[keys[-1]] = value
    return result


def get_config_path(spec: str | Path) -> Path:
    path = Path(spec)
    if path.suffix != ".yaml":
        path = path.with_suffix(".yaml")
    candidates = [Path(spec), path, builtin_config_dir / path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Could not find config file for {spec!r}")


def _merge(left: dict, right: dict) -> dict:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def get_config_from_spec(spec: str | Path) -> dict:
    if isinstance(spec, str) and "=" in spec:
        return _key_value_spec_to_nested_dict(spec)
    data = yaml.safe_load(get_config_path(spec).read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def load_config(config_path: str | Path | None = None, overrides: list[str] | tuple[str, ...] = ()) -> dict:
    config = get_config_from_spec(config_path or builtin_config_dir / "default.yaml")
    for override in overrides:
        config = _merge(config, get_config_from_spec(override))
    return config


__all__ = ["builtin_config_dir", "get_config_path", "get_config_from_spec", "load_config"]
