"""Agent implementations and dynamic factory."""

import copy
import importlib

from .. import Agent
from .default import DefaultAgent

_AGENT_MAPPING = {"default": "repo_agent.agents.default.DefaultAgent"}


def get_agent_class(spec: str) -> type[Agent]:
    full_path = _AGENT_MAPPING.get(spec, spec)
    try:
        module_name, class_name = full_path.rsplit(".", 1)
        return getattr(importlib.import_module(module_name), class_name)
    except (ValueError, ImportError, AttributeError) as error:
        raise ValueError(f"Unknown agent type: {spec} (resolved to {full_path}, available: {_AGENT_MAPPING})") from error


def get_agent(model, env, config: dict | None = None, *, default_type: str = "default") -> Agent:
    values = copy.deepcopy(config or {})
    agent_class = values.pop("agent_class", default_type)
    return get_agent_class(agent_class)(model, env, **values)


__all__ = ["DefaultAgent", "get_agent", "get_agent_class"]
