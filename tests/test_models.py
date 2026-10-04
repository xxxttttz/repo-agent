import copy
import http.client
import json
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
from repo_agent.models.base import (
    ACTION_JSON_SCHEMA,
    build_api_messages,
    parse_agent_action,
)


def test_parses_valid_action():
    assert parse_agent_action('{"content": "Inspecting", "command": "ls"}') == AgentAction("Inspecting", "ls")


@pytest.mark.parametrize("raw", ['{"content": "Done"}', '{"content": "Done", "command": null}'])
def test_rejects_invalid_commands(raw):
    with pytest.raises(ModelResponseError):
        parse_agent_action(raw)
    assert {"type": "string"} in ACTION_JSON_SCHEMA["properties"]["command"]["anyOf"]


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


@pytest.mark.parametrize("model_type", [OpenRouterModel, GroqModel, HuggingFaceModel])
@pytest.mark.parametrize("error_type", [http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError])
def test_provider_retries_connection_interruptions_without_replaying_actions(monkeypatch, model_type, error_type):
    delays = []
    monkeypatch.setattr("repo_agent.models.base.time.sleep", delays.append)
    model = model_type(model_name="test/model", api_key="test", max_retries=2)
    calls = []
    messages = [{"role": "user", "content": "Inspect app.py"}]

    def send(history):
        calls.append(copy.deepcopy(history))
        if len(calls) == 1:
            raise error_type("connection interrupted")
        return {"choices": [{"message": {"content": '{"content":"inspect","command":"cat app.py"}'}}]}

    model._send_request = send
    response = model.query(messages)
    assert response["extra"]["actions"] == [{"command": "cat app.py"}]
    assert calls == [messages, messages]
    assert delays == [1]


def test_connection_retries_stop_at_configured_limit_and_preserve_cause(monkeypatch):
    delays = []
    monkeypatch.setattr("repo_agent.models.base.time.sleep", delays.append)
    model = HuggingFaceModel(model_name="test/model", api_key="test", max_retries=3)
    calls = []

    def send(messages):
        calls.append(copy.deepcopy(messages))
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    model._send_request = send
    with pytest.raises(ModelTransportError, match="after 3 attempts: Remote end closed") as error:
        model.query([{"role": "user", "content": "task"}])
    assert len(calls) == 3
    assert delays == [1, 2]
    assert isinstance(error.value.__cause__, http.client.RemoteDisconnected)


@pytest.mark.parametrize("model_type", [OpenRouterModel, GroqModel, HuggingFaceModel])
def test_provider_history_preserves_the_command_that_actually_ran(model_type):
    model = model_type(model_name="test/model", api_key="test")
    previous = model.format_message("修改源码", [{"command": "sed -i 's/old/new/' app.py"}])
    messages = [{"role": "system", "content": "Use JSON"}, previous,
                {"role": "user", "content": "Command status: success\nOutput:\n"}]
    original = copy.deepcopy(messages)
    captured = []
    model._send_request = lambda api_messages: (captured.append(api_messages) or {
        "choices": [{"message": {"content": '{"content":"inspect edit","command":"cat app.py"}'}}],
    })
    model.query(messages)
    assert json.loads(captured[0][1]["content"]) == {
        "content": "修改源码", "command": "sed -i 's/old/new/' app.py",
    }
    assert messages == original
    assert captured[0][0] == messages[0]
    assert captured[0][2] == messages[2]


def test_plain_assistant_history_remains_compatible():
    messages = [{"role": "assistant", "content": "legacy explanation"}]
    assert build_api_messages(messages) == messages
