import json
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from jinja2 import StrictUndefined, Template

DEFAULT_OBSERVATION_TEMPLATE = (
    "Command status: {{ output.status.value if output.status is defined else output.status }}\n"
    "{% if output.returncode is defined and output.returncode is not none %}Return code: {{ output.returncode }}\n{% endif %}"
    "{% if output.error %}Error: {{ output.error }}\n{% endif %}"
    "{% if output.truncated %}Output truncated: true\n{% endif %}"
    "Output:\n{{ output.output }}"
)

ACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {"content": {"type": "string"}, "command": {"type": "string"}},
    "required": ["content", "command"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class AgentAction:
    content: str
    command: str | None


class ModelBackend(Protocol):
    def query(self, messages: list[dict]) -> dict: ...
    def format_message(self, content: str, actions: list[dict] | None = None) -> dict: ...
    def format_observation_messages(self, message: dict, outputs: list[Any], template_vars: dict | None = None) -> list[dict]: ...
    def get_template_vars(self, **kwargs) -> dict[str, Any]: ...
    def serialize(self) -> dict: ...


class ModelResponseError(ValueError):
    """Raised when a provider response does not match the provider action schema."""


class ModelTransportError(RuntimeError):
    """Raised when a provider request still fails after transient retries."""


class MessageModel:
    """Shared message formatting for the lightweight provider adapters."""

    def __init__(self, *, observation_template: str | None = None, **_kwargs):
        self.observation_template = observation_template or DEFAULT_OBSERVATION_TEMPLATE

    def format_message(self, content: str, actions: list[dict] | None = None) -> dict:
        return {"role": "assistant", "content": content, "extra": {"actions": actions or []}}

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        variables = {"model_name": getattr(self, "model_name", None),
                     "observation_template": self.observation_template}
        variables.update(kwargs)
        return variables

    def format_observation_messages(self, message: dict, outputs: list[Any], template_vars: dict | None = None) -> list[dict]:
        rendered = []
        for output in outputs:
            variables = {"message": message, "output": output}
            variables.update(template_vars or {})
            content = Template(self.observation_template, undefined=StrictUndefined).render(**variables)
            rendered.append({"role": "user", "content": content})
        return rendered

    def serialize(self) -> dict:
        return {"class": f"{type(self).__module__}.{type(self).__name__}", "model_name": getattr(self, "model_name", None)}


def build_api_messages(messages: list[dict]) -> list[dict]:
    return [{"role": message["role"], "content": message.get("content", "")} for message in messages]


def send_with_retry(
    send: Callable[[list[dict]], dict],
    messages: list[dict],
    *,
    max_retries: int,
    provider_name: str,
) -> dict:
    last_error: Exception | None = None
    attempts_made = 0
    for attempt in range(1, max_retries + 1):
        attempts_made = attempt
        try:
            return send(messages)
        except urllib.error.HTTPError as error:
            last_error = error
            transient = error.code == 429 or 500 <= error.code < 600
            if not transient or attempt == max_retries:
                break
            time.sleep(_retry_delay(error, attempt))
        except urllib.error.URLError as error:
            last_error = error
            if attempt == max_retries:
                break
            time.sleep(min(2 ** (attempt - 1), 8))
        except TimeoutError as error:
            last_error = error
            if attempt == max_retries:
                break
            time.sleep(min(2 ** (attempt - 1), 8))

    if isinstance(last_error, urllib.error.HTTPError):
        detail = f"HTTP {last_error.code} {last_error.reason}"
    else:
        detail = str(last_error)
    attempt_label = "attempt" if attempts_made == 1 else "attempts"
    raise ModelTransportError(
        f"{provider_name} request failed after {attempts_made} {attempt_label}: {detail}"
    ) from last_error


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), 30.0)
        except ValueError:
            pass
    return min(2 ** (attempt - 1), 8)


def parse_agent_action(raw_content: object, *, require_command: bool = False) -> AgentAction:
    if not isinstance(raw_content, str):
        raise ModelResponseError(f"Model content must be a string, got {type(raw_content).__name__}.")
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as error:
        raise ModelResponseError(f"Response is not valid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ModelResponseError("Response JSON must be an object.")
    required = {"content", "command"}
    if missing := required - payload.keys():
        raise ModelResponseError(f"Missing required field(s): {', '.join(sorted(missing))}.")
    if extra := payload.keys() - required:
        raise ModelResponseError(f"Unexpected field(s): {', '.join(sorted(extra))}.")
    if not isinstance(payload["content"], str):
        raise ModelResponseError("'content' must be a string.")
    if not isinstance(payload["command"], str):
        raise ModelResponseError("'command' must be a string.")
    return AgentAction(payload["content"], payload["command"])


def format_retry_message(error: Exception, raw_content: object) -> dict:
    return {"role": "user", "content": f"Invalid response: {error}\nPrevious response: {raw_content!r}\nReturn valid JSON."}
