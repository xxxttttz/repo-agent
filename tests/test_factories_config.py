from pathlib import Path
import re

import pytest

from repo_agent import Agent, Environment, Model, __version__
from repo_agent.agents import DefaultAgent, get_agent, get_agent_class
from repo_agent.config import builtin_config_dir, get_config_from_spec, load_config
from repo_agent.environments import LocalEnvironment, get_environment, get_environment_class
from repo_agent.models import GroqModel, HuggingFaceModel, MockModel, OpenRouterModel, get_model, get_model_class
from repo_agent.policies import CompletionContext, FileEvidenceCompletionPolicy
from repo_agent.run.local import _component_configs, build_parser


def test_shortcut_and_full_path_factories():
    assert get_agent_class("default") is DefaultAgent
    assert get_agent_class("repo_agent.agents.default.DefaultAgent") is DefaultAgent
    assert get_environment_class("local") is LocalEnvironment
    assert get_environment_class("repo_agent.environments.local.LocalEnvironment") is LocalEnvironment
    assert get_model_class("mock") is MockModel
    assert get_model_class("huggingface") is HuggingFaceModel
    assert get_model_class("repo_agent.models.mock.MockModel") is MockModel


def test_factories_create_components(tmp_path):
    environment = get_environment({"cwd": str(tmp_path)})
    model = get_model({"model_class": "mock"})
    agent = get_agent(model, environment, {"max_steps": 2})
    assert isinstance(environment, LocalEnvironment)
    assert isinstance(model, MockModel)
    assert isinstance(agent, DefaultAgent)


@pytest.mark.parametrize("model_class, expected", [
    ("openrouter", OpenRouterModel),
    ("groq", GroqModel),
    ("huggingface", HuggingFaceModel),
])
def test_configured_model_name_reaches_provider(model_class, expected):
    model = get_model({"model_class": model_class, "model_name": "test/model", "api_key": "test"})
    assert isinstance(model, expected)
    assert model.model_name == "test/model"


def test_run_maps_four_sections_and_nested_override(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("agent:\n  max_steps: 3\nenvironment:\n  cwd: /tmp\nmodel:\n  model_class: mock\n", encoding="utf-8")
    config = load_config(path, ["model.model_name=custom/name", "environment.timeout=1.5"])
    args = build_parser().parse_args(["task", "--workspace", str(tmp_path), "--max-steps", "8", "--provider", "mock"])
    agent_config, environment_config, model_config = _component_configs(config, args)
    assert agent_config["max_steps"] == 8
    assert environment_config["cwd"] == str(tmp_path)
    assert environment_config["timeout"] == 1.5
    assert model_config["model_class"] == "mock"
    assert model_config["model_name"] == "custom/name"


def test_config_loader_and_default_shape(tmp_path):
    custom = tmp_path / "custom.yaml"
    custom.write_text("agent:\n  max_steps: 2\n", encoding="utf-8")
    loaded = load_config(custom, ["agent.max_steps=7", "environment.timeout=1.5", "enabled=true"])
    assert loaded["agent"]["max_steps"] == 7
    assert loaded["environment"]["timeout"] == 1.5
    assert loaded["enabled"] is True
    assert get_config_from_spec("x.y=3") == {"x": {"y": 3}}
    default = load_config(builtin_config_dir / "default.yaml")
    assert set(default) == {"agent", "environment", "model", "run"}


def test_public_protocols_and_version_exist():
    assert hasattr(Agent, "run") and hasattr(Model, "query") and hasattr(Environment, "execute")
    assert re.match(r"^\d+\.\d+\.\d+$", __version__)


def test_completion_policy_prefers_exact_workspace_relative_path(tmp_path):
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    nested = tmp_path / ".cache"
    nested.mkdir()
    (nested / "README.md").write_text("# Cache\n", encoding="utf-8")
    environment = LocalEnvironment(str(tmp_path))
    policy = FileEvidenceCompletionPolicy()

    missing = policy.evaluate(CompletionContext("Read README.md", environment, ()))
    assert not missing.allowed
    assert "have not read" in missing.reason
    assert "ambiguous" not in missing.reason

    inspected = policy.evaluate(CompletionContext("Read README.md", environment, ("head README.md",)))
    assert inspected.allowed

    inspected_with_grep = policy.evaluate(
        CompletionContext("Read README.md", environment, ("grep -m1 '^#' README.md",))
    )
    assert inspected_with_grep.allowed
