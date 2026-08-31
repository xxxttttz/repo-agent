from .base import AgentAction, MessageModel, ModelBackend, ModelResponseError, ModelTransportError
from .groq import GroqModel
from .huggingface import HuggingFaceModel
from .mock import MockModel
from .openrouter import OpenRouterModel

import copy
import importlib

_MODEL_MAPPING = {
    "mock": "repo_agent.models.mock.MockModel",
    "openrouter": "repo_agent.models.openrouter.OpenRouterModel",
    "groq": "repo_agent.models.groq.GroqModel",
    "huggingface": "repo_agent.models.huggingface.HuggingFaceModel",
}


def get_model_class(spec: str) -> type:
    full_path = _MODEL_MAPPING.get(spec, spec)
    try:
        module_name, class_name = full_path.rsplit(".", 1)
        return getattr(importlib.import_module(module_name), class_name)
    except (ValueError, ImportError, AttributeError) as error:
        raise ValueError(f"Unknown model class: {spec} (resolved to {full_path}, available: {_MODEL_MAPPING})") from error


def get_model(config: dict | None = None, *, model_name: str | None = None) -> ModelBackend:
    values = copy.deepcopy(config or {})
    configured_model_name = values.pop("model_name", None)
    selected = model_name or configured_model_name or "mock"
    model_class = values.pop("model_class", selected if selected in _MODEL_MAPPING else "mock")
    if model_class != "mock" and (model_name or configured_model_name):
        values["model_name"] = model_name or configured_model_name
    if model_class == "mock":
        values.pop("model_name", None)
    return get_model_class(model_class)(**values)

__all__ = [
    "AgentAction",
    "GroqModel",
    "HuggingFaceModel",
    "MockModel",
    "ModelBackend",
    "MessageModel",
    "ModelResponseError",
    "ModelTransportError",
    "OpenRouterModel",
    "get_model",
    "get_model_class",
]
