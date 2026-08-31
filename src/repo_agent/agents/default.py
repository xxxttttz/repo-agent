"""Default linear-trajectory evidence-aware agent."""

import copy
import json
import re
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined, Template

from .. import __version__
from ..environments.local import ExecutionResult, ExecutionStatus, LocalEnvironment
from ..models import AgentAction, ModelBackend
from ..policies import CompletionContext, CompletionPolicy, FileEvidenceCompletionPolicy
from ..result import AgentResult, AgentStatus, AgentStep


class DefaultAgent:
    def __init__(self, model: ModelBackend, env: LocalEnvironment, max_steps: int = 5,
                 completion_policy: CompletionPolicy | None = None, system_template: str | None = None,
                 instance_template: str | None = None, component_config: dict | None = None):
        self.model = model
        self.env = env
        self.max_steps = max_steps
        self.completion_policy = completion_policy or FileEvidenceCompletionPolicy()
        self.system_template = system_template or "You are a coding agent. Use shell actions and submit with the completion marker."
        self.instance_template = instance_template or "Task: {{ task }}"
        self.component_config = copy.deepcopy(component_config or {})
        self.messages: list[dict] = []
        self._last_result: AgentResult | None = None
        self._task: str | None = None
        self.resumed_from_step = 0

    @staticmethod
    def _render(template: str, **variables: Any) -> str:
        return Template(template, undefined=StrictUndefined).render(**variables)

    def run(self, task: str) -> AgentResult:
        self._task = task
        self.resumed_from_step = 0
        self.messages = []
        variables = {}
        variables.update(self.env.get_template_vars())
        variables.update(self.model.get_template_vars())
        variables.update({"max_steps": self.max_steps})
        variables.update({"task": task})  # task is always the caller's value
        self.messages.extend([
            {"role": "system", "content": self._render(self.system_template, **variables)},
            {"role": "user", "content": self._render(self.instance_template, **variables)},
        ])
        return self._run_steps(task, [])

    def resume(self, task: str, trajectory: dict) -> AgentResult:
        if trajectory.get("status") == AgentStatus.COMPLETED.value:
            raise ValueError("Cannot resume a completed trajectory.")
        messages = trajectory.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Trajectory must contain a non-empty messages list.")

        self._task = task
        self.messages = copy.deepcopy(messages)
        prior_error = None
        while self.messages and self.messages[-1].get("role") == "exit":
            prior_error = self.messages.pop().get("content")

        steps_data = trajectory.get("steps")
        if isinstance(steps_data, list):
            steps = [AgentStep.deserialize(step) for step in steps_data]
        else:
            steps = self._infer_steps(self.messages)
        self.resumed_from_step = len(steps)

        resume_message = (
            "Resume the same task from this trajectory. Preserve successful "
            "work and use the existing observations as evidence. Do not repeat "
            "completed work unnecessarily. The workspace may have changed since "
            "the trajectory was saved, so inspect every relevant current file "
            "before editing and never overwrite unobserved changes. "
            f"You have up to {self.max_steps} additional steps."
        )
        if prior_error:
            resume_message += f" The previous run stopped with this error: {prior_error}"
        self.messages.append({"role": "user", "content": resume_message})
        return self._run_steps(task, steps)

    def _run_steps(self, task: str, existing_steps: list[AgentStep]) -> AgentResult:
        steps = list(existing_steps)
        successful_commands = [
            step.command
            for step in steps
            if step.command is not None
            and step.execution_status is ExecutionStatus.SUCCESS
            and step.submission is None
        ]
        last_content = next(
            (str(message.get("content", "")) for message in reversed(self.messages)
             if message.get("role") == "assistant"),
            "",
        )
        first_step_number = len(steps) + 1
        for step_number in range(first_step_number, first_step_number + self.max_steps):
            try:
                raw_message = self.model.query(self.messages)
            except Exception as error:
                self.messages.append({
                    "role": "exit",
                    "content": str(error),
                    "extra": {
                        "exit_status": "error",
                        "exception_type": type(error).__name__,
                    },
                })
                result = AgentResult(
                    AgentStatus.ERROR,
                    str(error),
                    tuple(steps),
                    tuple(self.messages),
                )
                self._last_result = result
                return result
            if isinstance(raw_message, AgentAction):  # compatibility with pre-stage custom models
                actions = [] if raw_message.command is None else [{"command": raw_message.command}]
                raw_message = self.model.format_message(raw_message.content, actions)
            message = copy.deepcopy(raw_message)
            message.setdefault("role", "assistant")
            self.messages.append(message)
            last_content = str(message.get("content", ""))
            actions = message.get("extra", {}).get("actions", [])
            command = actions[0].get("command") if actions and isinstance(actions[0], dict) else None
            if not isinstance(command, str):
                observation = {"role": "user", "content": "No shell action was provided. Continue with a command."}
                self.messages.append(observation)
                steps.append(AgentStep(step_number, last_content, None, completion_rejection="No submission action was provided."))
                continue

            execution = self.env.execute(message)
            if execution.status is ExecutionStatus.SUCCESS and execution.submission is None:
                successful_commands.append(command)
            observation_messages = self.model.format_observation_messages(message, [execution])
            self.messages.extend(copy.deepcopy(observation_messages))
            step = AgentStep(step_number, last_content, command, execution.output, execution.returncode,
                             execution.status, execution.error, execution.truncated, None, execution.submission)
            steps.append(step)
            if execution.submission is not None:
                decision = self.completion_policy.evaluate(CompletionContext(
                    task=task, environment=self.env, successful_commands=tuple(successful_commands)))
                if decision.allowed:
                    answer = execution.submission or last_content
                    result = AgentResult(AgentStatus.COMPLETED, answer, tuple(steps), tuple(self.messages))
                    self._last_result = result
                    return result
                rejection = {
                    "role": "user",
                    "content": (
                        f"Completion rejected: {decision.reason}\n"
                        "Your next command must be a non-submission shell command "
                        "that addresses this reason. Do not repeat the completion "
                        "marker until you have new successful evidence."
                    ),
                }
                self.messages.append(rejection)
                steps[-1] = AgentStep(step.number, step.content, step.command, step.output, step.returncode,
                                      step.execution_status, step.error, step.output_truncated, decision.reason, step.submission)

        result = AgentResult(AgentStatus.MAX_STEPS, last_content, tuple(steps), tuple(self.messages))
        self._last_result = result
        return result

    @staticmethod
    def _infer_steps(messages: list[dict]) -> list[AgentStep]:
        """Reconstruct basic step evidence from pre-steps-field trajectories."""
        steps = []
        for index, message in enumerate(messages):
            if message.get("role") != "assistant":
                continue
            actions = message.get("extra", {}).get("actions", [])
            command = actions[0].get("command") if actions and isinstance(actions[0], dict) else None
            if not isinstance(command, str):
                continue

            observation = messages[index + 1] if index + 1 < len(messages) else {}
            observation_content = str(observation.get("content", ""))
            status_match = re.search(r"^Command status: (\w+)$", observation_content, re.MULTILINE)
            returncode_match = re.search(r"^Return code: (-?\d+)$", observation_content, re.MULTILINE)
            status = None
            if status_match:
                try:
                    status = ExecutionStatus(status_match.group(1))
                except ValueError:
                    pass
            output = observation_content.split("Output:\n", 1)[1] if "Output:\n" in observation_content else None
            submission = None
            if status is ExecutionStatus.SUCCESS and output is not None:
                lines = output.splitlines()
                if lines and lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT":
                    submission = "\n".join(lines[1:]).strip()
            rejection = None
            if index + 2 < len(messages):
                following = str(messages[index + 2].get("content", ""))
                if following.startswith("Completion rejected: "):
                    rejection = following.split("\n", 1)[0].removeprefix("Completion rejected: ")
            steps.append(AgentStep(
                len(steps) + 1,
                str(message.get("content", "")),
                command,
                output,
                int(returncode_match.group(1)) if returncode_match else None,
                status,
                completion_rejection=rejection,
                submission=submission,
            ))
        return steps

    def serialize(self) -> dict:
        result = self._last_result
        return {"version": __version__, "status": result.status.value if result else None,
                "task": self._task, "answer": result.answer if result else "",
                "steps": [step.serialize() for step in result.steps] if result else [],
                "messages": list(result.messages) if result else [],
                "component_config": self._redact(copy.deepcopy(self.component_config) or {
                    "agent": {"class": f"{type(self).__module__}.{type(self).__name__}", "max_steps": self.max_steps},
                    "environment": self.env.serialize(), "model": self.model.serialize(),
                })}

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: "[REDACTED]" if any(word in str(key).lower() for word in ("api_key", "token", "secret", "password"))
                    else cls._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        if isinstance(value, tuple):
            return [cls._redact(item) for item in value]
        return value

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(json.dumps(self.serialize(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
