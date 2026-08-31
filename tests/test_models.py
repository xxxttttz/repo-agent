import copy
import urllib.error

import pytest

from repo_agent.models import (
    AgentAction,
    GroqModel,
    HuggingFaceModel,
    MockModel,
    ModelResponseError,
    ModelTransportError,
    OpenRouterModel,
)
from repo_agent.models.base import ACTION_JSON_SCHEMA, parse_agent_action


def test_parses_valid_action():
    assert parse_agent_action('{"content": "Inspecting", "command": "ls"}') == AgentAction("Inspecting", "ls")


@pytest.mark.parametrize("raw", ['{"content": "Done"}', '{"content": "Done", "command": null}'])
def test_rejects_invalid_commands(raw):
    with pytest.raises(ModelResponseError):
        parse_agent_action(raw)
    assert ACTION_JSON_SCHEMA["properties"]["command"] == {"type": "string"}


def test_mock_returns_one_command_then_submission():
    model = MockModel()
    first = model.query([{"role": "user", "content": "task"}])
    second = model.query([{"role": "user", "content": "task"}, first, {"role": "user", "content": "obs"}])
    assert first["extra"]["actions"] == [{"command": "ls -la"}]
    assert second["extra"]["actions"] == [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]


@pytest.mark.parametrize("model_type, response", [
    (OpenRouterModel, '{"content":"ok","command":"ls"}'),
    (GroqModel, '{"content":"ok","command":"ls"}'),
    (HuggingFaceModel, '{"content":"ok","command":"ls"}'),
])
def test_provider_query_contract_and_trajectory(model_type, response):
    model = model_type(model_name="test/model", api_key="test")
    captured = []
    model._send_request = lambda messages: (captured.append(copy.deepcopy(messages)) or
        {"choices": [{"message": {"content": response}}]})
    trajectory = [{"role": "system", "content": "system from trajectory"}, {"role": "user", "content": "task"}]
    result = model.query(trajectory)
    assert result["role"] == "assistant"
    assert result["extra"]["actions"] == [{"command": "ls"}]
    assert captured[0] == trajectory
    assert sum(message["role"] == "system" for message in captured[0]) == 1


def test_groq_null_command_retries_and_is_rejected():
    model = GroqModel(model_name="test/model", api_key="test")
    calls = []
    def send(messages):
        calls.append(copy.deepcopy(messages))
        content = '{"content":"bad","command":null}' if len(calls) == 1 else '{"content":"ok","command":"ls"}'
        return {"choices": [{"message": {"content": content}}]}
    model._send_request = send
    result = model.query([{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}])
    assert result["extra"]["actions"] == [{"command": "ls"}]
    assert len(calls) == 2


def test_huggingface_defaults_to_inference_providers(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "test-token")
    model = HuggingFaceModel()
    assert model.model_name == "Qwen/Qwen2.5-Coder-32B-Instruct:nscale"
    assert model.max_tokens == 1024
    assert model.timeout == 120.0
    assert model.API_URL == "https://router.huggingface.co/v1/chat/completions"
    assert model.api_key == "test-token"


def test_openrouter_retries_rate_limit_without_network():
    model = OpenRouterModel(model_name="test/model", api_key="test")
    calls = []

    def send(messages):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                "https://openrouter.ai/api/v1/chat/completions",
                429,
                "Too Many Requests",
                {"Retry-After": "0"},
                None,
            )
        return {"choices": [{"message": {"content": '{"content":"ok","command":"ls"}'}}]}

    model._send_request = send
    result = model.query([{"role": "system", "content": "sys"}])
    assert result["extra"]["actions"] == [{"command": "ls"}]
    assert len(calls) == 2


def test_huggingface_retries_timeout_without_network():
    model = HuggingFaceModel(model_name="test/model", api_key="test", max_retries=2)
    calls = []

    def send(messages):
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            raise TimeoutError("read operation timed out")
        return {"choices": [{"message": {"content": '{"content":"ok","command":"ls"}'}}]}

    model._send_request = send
    result = model.query([{"role": "system", "content": "sys"}])
    assert result["extra"]["actions"] == [{"command": "ls"}]
    assert len(calls) == 2


def test_non_transient_http_error_is_not_retried():
    model = GroqModel(model_name="test/model", api_key="test", max_retries=3)
    calls = []

    def send(messages):
        calls.append(messages)
        raise urllib.error.HTTPError(
            "https://api.groq.com/openai/v1/chat/completions",
            403,
            "Forbidden",
            {},
            None,
        )

    model._send_request = send
    with pytest.raises(ModelTransportError, match="after 1 attempt: HTTP 403 Forbidden"):
        model.query([{"role": "system", "content": "sys"}])
    assert len(calls) == 1
