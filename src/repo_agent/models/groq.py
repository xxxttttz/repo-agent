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


class GroqModel(MessageModel):
    API_URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(
        self,
        model_name: str = "openai/gpt-oss-20b",
        max_retries: int = 3,
        api_key: str | None = None,
        observation_template: str | None = None,
    ):
        super().__init__(observation_template=observation_template)
        self.model_name = model_name
        self.max_retries = max_retries
        self.api_key = api_key or os.getenv("GROQ_API_KEY")

        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not set.")

    def query(self, messages: list[dict]) -> dict:
        retry_messages = build_api_messages(messages)
        last_error: ModelResponseError | None = None
        last_raw_content: object = None

        for _attempt in range(1, self.max_retries + 1):
            data = send_with_retry(
                self._send_request,
                retry_messages,
                max_retries=self.max_retries,
                provider_name="Groq",
            )
            raw_content = data["choices"][0]["message"].get("content")
            last_raw_content = raw_content

            try:
                action = parse_agent_action(
                    raw_content,
                    require_command=True,
                )
                return self.format_message(action.content, [{"command": action.command}])
            except ModelResponseError as error:
                last_error = error
                retry_messages.append(
                    format_retry_message(error, raw_content)
                )

        raise RuntimeError(
            "Groq failed to return a valid response after "
            f"{self.max_retries} attempts. Last error: {last_error}. "
            f"Last response: {str(last_raw_content)[:1000]!r}"
        )

    def _send_request(self, messages: list[dict]) -> dict:
        body = {
            "model": self.model_name,
            "messages": messages,
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

        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
