from .base import MessageModel


class MockModel(MessageModel):
    def query(self, messages: list[dict]) -> dict:
        has_command = any(message.get("role") == "assistant" and message.get("extra", {}).get("actions") for message in messages)
        if not has_command:
            return self.format_message("I will list the current directory.", [{"command": "ls -la"}])
        return self.format_message("Submitting the result.", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])
