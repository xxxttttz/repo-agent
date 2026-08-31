import json
import os
import urllib.request

from .base import (
    ACTION_JSON_SCHEMA,
    MessageModel,
    ModelResponseError,
    build_api_messages,
    format_retry_message,
    parse_agent_action,
    send_with_retry,
)


class HuggingFaceModel(MessageModel):
    """OpenAI-compatible Hugging Face Inference Providers adapter."""

    API_URL = "https://router.huggingface.co/v1/chat/completions"

    def __init__(
        self,
        model_name: str = "Qwen/Qwen2.5-Coder-32B-Instruct:nscale",
        max_retries: int = 3,
        max_tokens: int = 1024,
        timeout: float = 120.0,
        api_key: str | None = None,
        observation_template: str | None = None,
    ):
        super().__init__(observation_template=observation_template)
        self.model_name = model_name
        self.max_retries = max_retries
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.api_key = api_key or os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACEHUB_API_TOKEN")

        if not self.api_key:
            raise RuntimeError("HF_TOKEN is not set.")

    def query(self, messages: list[dict]) -> dict:
        retry_messages = build_api_messages(messages)
        last_error: ModelResponseError | None = None
        last_raw_content: object = None

        for _attempt in range(1, self.max_retries + 1):
            data = send_with_retry(
                self._send_request,
                retry_messages,
                max_retries=self.max_retries,
                provider_name="Hugging Face",
            )
            raw_content = data["choices"][0]["message"].get("content")
            last_raw_content = raw_content

            try:
                action = parse_agent_action(raw_content, require_command=True)
                return self.format_message(action.content, [{"command": action.command}])
            except ModelResponseError as error:
                last_error = error
                retry_messages.append(format_retry_message(error, raw_content))

        raise RuntimeError(
            "Hugging Face failed to return a valid response after "
            f"{self.max_retries} attempts. Last error: {last_error}. "
            f"Last response: {str(last_raw_content)[:1000]!r}"
        )

    def _send_request(self, messages: list[dict]) -> dict:
        body = {
            "model": self.model_name,
            "messages": messages,
            # Reasoning models may consume a provider's small default output
            # budget before producing message.content.
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_action",
                    "strict": True,
                    "schema": ACTION_JSON_SCHEMA,
                },
            },
        }
        request = urllib.request.Request(
            self.API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))
