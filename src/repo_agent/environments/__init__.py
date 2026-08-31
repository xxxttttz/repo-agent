"""Environment implementations and dynamic factory."""

import copy
import importlib

from .. import Environment
from .local import DangerousCommandPolicy, ExecutionResult, ExecutionStatus, LocalEnvironment

_ENVIRONMENT_MAPPING = {"local": "repo_agent.environments.local.LocalEnvironment"}


def get_environment_class(spec: str) -> type[Environment]:
    full_path = _ENVIRONMENT_MAPPING.get(spec, spec)
    try:
        module_name, class_name = full_path.rsplit(".", 1)
        return getattr(importlib.import_module(module_name), class_name)
    except (ValueError, ImportError, AttributeError) as error:
        raise ValueError(f"Unknown environment type: {spec} (resolved to {full_path}, available: {_ENVIRONMENT_MAPPING})") from error


def get_environment(config: dict | None = None, *, default_type: str = "local") -> Environment:
    values = copy.deepcopy(config or {})
    environment_class = values.pop("environment_class", default_type)
    return get_environment_class(environment_class)(**values)


__all__ = ["DangerousCommandPolicy", "ExecutionResult", "ExecutionStatus", "LocalEnvironment", "get_environment", "get_environment_class"]
