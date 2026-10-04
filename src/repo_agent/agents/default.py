"""Default linear-trajectory evidence-aware agent."""

import copy
import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined, Template

from .. import __version__
from ..environments.local import ExecutionResult, ExecutionStatus, LocalEnvironment
from ..models import AgentAction, ModelBackend
from ..policies import CompletionContext, CompletionPolicy, FileEvidenceCompletionPolicy
from ..policies.protected import ProtectedFiles
from ..result import AgentResult, AgentStatus, AgentStep


class DefaultAgent:
    def __init__(self, model: ModelBackend, env: LocalEnvironment, max_steps: int = 5,
                 completion_policy: CompletionPolicy | None = None, system_template: str | None = None,
                 instance_template: str | None = None, component_config: dict | None = None,
                 retrieval_context: str = "", conversation_context: str = "",
                 verification_commands: list[str] | tuple[str, ...] = (),
                 protected_paths: list[str] | tuple[str, ...] = (),
                 cancellation_check: Callable[[], bool] | None = None):
        self.model = model
        self.env = env
        self.max_steps = max_steps
        self.completion_policy = completion_policy or FileEvidenceCompletionPolicy()
        self.system_template = system_template or "You are a coding agent. Use shell actions and submit with the completion marker."
        self.instance_template = instance_template or "Task: {{ task }}"
        self.component_config = copy.deepcopy(component_config or {})
        self.retrieval_context = retrieval_context
        self.conversation_context = conversation_context
        self.cancellation_check = cancellation_check or (lambda: False)
        if (not isinstance(verification_commands, (list, tuple))
                or any(not isinstance(command, str) or not command.strip()
                       for command in verification_commands)):
            raise ValueError("verification_commands must be a list of non-empty shell commands")
        self.verification_commands = tuple(verification_commands)
        self.verifications: list[dict] = []
        self.protected_files = ProtectedFiles(env.cwd, protected_paths)
        self.protected_baseline: dict[str, str] = {}
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
        self.verifications = []
        self.protected_baseline = self.protected_files.capture()
        variables = {}
        variables.update(self.env.get_template_vars())
        variables.update(self.model.get_template_vars())
        variables.update({"max_steps": self.max_steps})
        variables.update({"task": task})  # task is always the caller's value
        variables.update({"retrieval_context": self.retrieval_context})
        variables.update({"conversation_context": self.conversation_context})
        self.messages.extend([
            {"role": "system", "content": self._render(self.system_template, **variables)},
            {"role": "user", "content": self._render(self.instance_template, **variables)},
        ])
        self._describe_verification()
        self._describe_file_evidence(task, ())
        return self._run_steps(task, [])

    def resume(self, task: str, trajectory: dict) -> AgentResult:
        if trajectory.get("status") == AgentStatus.COMPLETED.value:
            raise ValueError("Cannot resume a completed trajectory.")
        messages = trajectory.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Trajectory must contain a non-empty messages list.")

        self._task = task
        self.messages = copy.deepcopy(messages)
        self.verifications = copy.deepcopy(trajectory.get("verifications", []))
        baseline = trajectory.get("protected_files", {})
        if (not isinstance(baseline, dict)
                or any(not isinstance(baseline.get(path), str) for path in self.protected_files.paths)):
            raise ValueError("Trajectory lacks the original snapshot for configured protected files")
        self.protected_baseline = {path: baseline[path] for path in self.protected_files.paths}
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
        self._describe_verification()
        self._describe_file_evidence(task, tuple(
            step.command for step in steps if isinstance(step.command, str)
            and step.execution_status is ExecutionStatus.SUCCESS and step.submission is None))
        return self._run_steps(task, steps)

    def _describe_file_evidence(self, task: str, successful_commands: tuple[str, ...]) -> None:
        """Explain the built-in evidence contract before spending model actions."""
        if type(self.completion_policy) is not FileEvidenceCompletionPolicy:
            return
        decision = self.completion_policy.evaluate(CompletionContext(task, self.env, successful_commands))
        if decision.required_commands:
            self.messages.append({
                "role": "user",
                "content": (
                    "File-evidence requirements before submission:\n"
                    + decision.reason
                    + "\nOnly successful standalone read commands count for this policy. "
                    "Reads combined with &&, ||, pipes, redirections or command substitutions "
                    "do not count, even if the overall command succeeds. Reading several files "
                    "in a single standalone cat command is allowed. Existing successful standalone "
                    "reads remain evidence; do not redo completed edits or recreate existing tests."
                ),
            })

    def _hint_on_submission_recovery(self, task: str, steps: list[AgentStep],
                                     successful_commands: list[str]) -> None:
        """Explain evidence progress after a rejection without accepting the task."""
        if type(self.completion_policy) is not FileEvidenceCompletionPolicy:
            return
        latest = steps[-1]
        if (latest.execution_status is not ExecutionStatus.SUCCESS or latest.submission is not None
                or not isinstance(latest.command, str)):
            return
        rejected = next((step for step in reversed(steps[:-1]) if step.submission is not None), None)
        if rejected is None or not rejected.completion_rejection:
            return
        before = self.completion_policy.evaluate(CompletionContext(
            task, self.env, tuple(successful_commands[:-1])))
        after = self.completion_policy.evaluate(CompletionContext(task, self.env, tuple(successful_commands)))
        if not before.required_commands or before.required_commands == after.required_commands:
            return
        if after.allowed:
            content = (
                "Submission recovery: the required file-reading evidence is now complete. "
                "This is not task acceptance and does not prove the implementation is correct. "
                "If the requested work is actually finished, submit the complete final summary "
                "with the independent completion marker as your next action; do not repeat "
                "completed edits, recreate existing tests, or spend actions merely echoing status."
            )
            if self.verification_commands:
                content += " The runner will rerun all required checks on submission; they must still pass."
            else:
                content += " If you changed code, run relevant tests after the last change before submitting."
        else:
            content = "Submission recovery: file evidence still blocks submission.\n" + after.reason
        self.messages.append({"role": "user", "content": content})

    def _describe_verification(self) -> None:
        if self.protected_files.paths:
            self.messages.append({
                "role": "user",
                "content": "These caller-protected files must remain unchanged from the start of "
                           "the task. Completion will be rejected if their contents, file type, "
                           "or permissions differ. Put summaries in your final answer, not in these files:\n"
                           + "\n".join(self.protected_files.paths),
            })
        if self.verification_commands:
            self.messages.append({
                "role": "user",
                "content": "Before accepting each submission, the runner will execute these "
                           "required checks in order. All must pass:\n"
                           + "\n".join(self.verification_commands),
            })

    def _verify_submission(self, step_number: int) -> str | None:
        """Re-run trusted caller-configured checks; never reuse old successes."""
        for command in self.verification_commands:
            if self.cancellation_check():
                return "Verification cancelled."
            execution = self.env.execute(command)
            self.verifications.append({
                "submission_step": step_number,
                "command": command,
                "status": execution.status.value,
                "returncode": execution.returncode,
                "output": execution.output,
                "error": execution.error,
                "truncated": execution.truncated,
            })
            self.messages.append({
                "role": "user",
                "content": f"Required verification: {command}\n"
                           f"Status: {execution.status.value}; return code: {execution.returncode}\n"
                           f"Error: {execution.error or ''}\n"
                           f"Output truncated: {execution.truncated}\n{execution.output}",
            })
            if execution.status is not ExecutionStatus.SUCCESS:
                return f"Required verification failed: {command}. Fix the cause before resubmitting."
        return None

    def _hint_on_repeated_reads(self, steps: list[AgentStep]) -> None:
        """Nudge repeated inspection without blocking edits or repeated tests."""
        readers = ("cat ", "head ", "tail ", "nl ", "sed ", "grep ", "rg ")
        recent = steps[-6:]
        latest = recent[-1]
        if (latest.execution_status is not ExecutionStatus.SUCCESS
                or latest.submission is not None or not latest.output or not latest.output.strip()
                or not isinstance(latest.command, str) or not latest.command.lstrip().startswith(readers)):
            return
        matches = sum(
            step.execution_status is ExecutionStatus.SUCCESS
            and step.submission is None and isinstance(step.command, str)
            and step.command.lstrip().startswith(readers)
            and (step.output or "").strip() == latest.output.strip()
            for step in recent
        )
        if matches == 3:
            self.messages.append({
                "role": "user",
                "content": (
                    "Inspection is repeating: three recent read commands returned the same text. "
                    "Changing grep context on the same file has not supplied new evidence. "
                    "Change the investigation target: read the complete relevant implementation "
                    "(for example, cat the source file), including validation and error paths, "
                    "or inspect a different related file. Use the task's source files to resolve "
                    "unanswered questions; do not assume the README describes every behavior."
                ),
            })

    def _hint_on_budget(self, remaining: int, task: str, successful_commands: list[str]) -> None:
        """Reserve room for validation/submission without forcing completion."""
        if self.max_steps < 4 or remaining not in (3, 1):
            return
        content = (
            f"Step budget: {remaining} model action(s) remain in this run, including the next action. "
            "Avoid optional refactoring and repeated inspection. Finish necessary changes, "
            "inspect required task files, and reserve an action for the independent completion "
            "marker. Only submit if the task is actually solved; otherwise use the remaining "
            "actions for useful work and report what is still unverified. Budget exhaustion "
            "does not count as completion."
        )
        # Do not call arbitrary caller policies outside their submission hook.
        if type(self.completion_policy) is FileEvidenceCompletionPolicy:
            decision = self.completion_policy.evaluate(CompletionContext(
                task=task, environment=self.env, successful_commands=tuple(successful_commands)))
            if not decision.allowed:
                content += f" Current file-evidence blocker: {decision.reason}"
        if self.verification_commands:
            content += (
                " Caller-required verification runs automatically on submission, outside the "
                "model-action budget. You do not need to spend an action duplicating those "
                "checks; a failed check still rejects completion."
            )
        else:
            content += " Run relevant tests after the last code change before submitting."
        self.messages.append({"role": "user", "content": content})

    def _run_steps(self, task: str, existing_steps: list[AgentStep]) -> AgentResult:
        steps = list(existing_steps)
        successful_commands = [
            step.command
            for step in steps
            if isinstance(step.command, str)
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
            if self.cancellation_check():
                return self._cancelled_result(steps)
            remaining = first_step_number + self.max_steps - step_number
            self._hint_on_budget(remaining, task, successful_commands)
            try:
                raw_message = self.model.query(self.messages)
            except Exception as error:  # noqa: BLE001 - provider failures become AgentResult.ERROR.
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
                return self._finish_result(result)
            if isinstance(raw_message, AgentAction):  # compatibility with pre-stage custom models
                actions = [] if raw_message.command is None else [{"command": raw_message.command}]
                raw_message = self.model.format_message(raw_message.content, actions)
            message = copy.deepcopy(raw_message)
            message.setdefault("role", "assistant")
            self.messages.append(message)
            last_content = str(message.get("content", ""))
            actions = message.get("extra", {}).get("actions", [])
            command = actions[0].get("command") if actions and isinstance(actions[0], dict) else None
            if not isinstance(command, (str, dict)):
                observation = {"role": "user", "content": "No action was provided. Continue with a shell or edit command."}
                self.messages.append(observation)
                steps.append(AgentStep(step_number, last_content, None, completion_rejection="No submission action was provided."))
                continue

            if (isinstance(command, dict) and isinstance(command.get("path"), str)
                    and Path(command["path"]).as_posix() in self.protected_files.paths):
                execution = ExecutionResult(ExecutionStatus.REJECTED,
                                            error=f"Edit rejected: {command['path']} is a caller-protected file.")
            else:
                execution = self.env.execute(message)
            if (isinstance(command, str) and execution.status is ExecutionStatus.SUCCESS
                    and execution.submission is None):
                successful_commands.append(command)
            observation_messages = self.model.format_observation_messages(message, [execution])
            self.messages.extend(copy.deepcopy(observation_messages))
            step = AgentStep(step_number, last_content, command, execution.output, execution.returncode,
                             execution.status, execution.error, execution.truncated, None, execution.submission)
            steps.append(step)
            self._hint_on_repeated_reads(steps)
            self._hint_on_submission_recovery(task, steps, successful_commands)
            if self.cancellation_check():
                return self._cancelled_result(steps)
            if execution.submission is not None:
                decision = self.completion_policy.evaluate(CompletionContext(
                    task=task, environment=self.env, successful_commands=tuple(successful_commands)))
                reason = decision.reason
                if decision.allowed:
                    reason = self.protected_files.check(self.protected_baseline)
                    if not reason:
                        reason = self._verify_submission(step_number)
                    if not reason:
                        reason = self.protected_files.check(self.protected_baseline)
                    if self.cancellation_check():
                        return self._cancelled_result(steps)
                if decision.allowed and not reason:
                    answer = execution.submission or last_content
                    result = AgentResult(AgentStatus.COMPLETED, answer, tuple(steps), tuple(self.messages))
                    return self._finish_result(result)
                rejection = {
                    "role": "user",
                    "content": (
                        f"Completion rejected: {reason}\n"
                        "Your next command must be a non-submission shell command "
                        "that addresses this reason. Do not repeat the completion "
                        "marker until you have new successful evidence."
                        " Address only the reported blockers: preserve completed edits and "
                        "existing regression tests instead of recreating them. Once the blockers "
                        "are resolved and the task is actually solved, submit again promptly; "
                        "a status-only echo is not a submission."
                        " When you resubmit, provide the complete task summary again, including "
                        "the actual changes or findings, verification evidence, and any remaining "
                        "limitations. Do not replace the summary with a note about resubmitting."
                    ),
                }
                self.messages.append(rejection)
                steps[-1] = AgentStep(step.number, step.content, step.command, step.output, step.returncode,
                                      step.execution_status, step.error, step.output_truncated, reason, step.submission)

        result = AgentResult(AgentStatus.MAX_STEPS, last_content, tuple(steps), tuple(self.messages))
        return self._finish_result(result)

    def _finish_result(self, result: AgentResult) -> AgentResult:
        """Attach runner receipts without rewriting or endorsing model prose."""
        accepted = result.status is AgentStatus.COMPLETED
        submission_step = next((step.number for step in reversed(result.steps)
                                if step.submission is not None), None)
        checks = [{key: copy.deepcopy(check.get(key)) for key in (
            "command", "status", "returncode", "error", "truncated")}
            for check in self.verifications if check.get("submission_step") == submission_step]
        if not self.verification_commands:
            verification_state = "not_configured"
        elif not checks:
            verification_state = "not_run"
        elif not accepted:
            verification_state = "not_accepted"
        elif ([check["command"] for check in checks] == list(self.verification_commands)
              and all(check["status"] == ExecutionStatus.SUCCESS.value for check in checks)):
            verification_state = "passed"
        else:
            verification_state = "incomplete"
        handoff = {
            "status": result.status.value,
            "submission_accepted": accepted,
            "successful_edit_actions": [
                {"step": step.number, "path": step.command.get("path"), "mode": step.command.get("mode")}
                for step in result.steps if isinstance(step.command, dict)
                and step.command.get("type") == "edit"
                and step.execution_status is ExecutionStatus.SUCCESS
            ],
            "verification": {"state": verification_state, "submission_step": submission_step,
                             "configured_commands": list(self.verification_commands), "checks": checks},
            "protected_files": {"paths": list(self.protected_files.paths),
                                "checked_on_accepted_submission": accepted and bool(self.protected_files.paths)},
            "limitations": [
                "Edit actions are historical receipts, not a final workspace diff; shell changes are not listed.",
                "Caller-selected checks do not prove all requirements or the accuracy of the model summary.",
            ],
        }
        result = replace(result, handoff=handoff)
        self._last_result = result
        return result

    def _cancelled_result(self, steps: list[AgentStep]) -> AgentResult:
        message = "Task cancelled by request."
        self.messages.append({
            "role": "exit",
            "content": message,
            "extra": {"exit_status": AgentStatus.CANCELLED.value},
        })
        result = AgentResult(
            AgentStatus.CANCELLED,
            message,
            tuple(steps),
            tuple(self.messages),
        )
        return self._finish_result(result)

    @staticmethod
    def _infer_steps(messages: list[dict]) -> list[AgentStep]:
        """Reconstruct basic step evidence from pre-steps-field trajectories."""
        steps = []
        for index, message in enumerate(messages):
            if message.get("role") != "assistant":
                continue
            actions = message.get("extra", {}).get("actions", [])
            command = actions[0].get("command") if actions and isinstance(actions[0], dict) else None
            if not isinstance(command, (str, dict)):
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
        component_config = copy.deepcopy(self.component_config) or {
            "agent": {"class": f"{type(self).__module__}.{type(self).__name__}", "max_steps": self.max_steps},
            "environment": self.env.serialize(), "model": self.model.serialize(),
        }
        component_config.setdefault("agent", {}).update({
            "verification_commands": list(self.verification_commands),
            "protected_paths": list(self.protected_files.paths),
        })
        return {"version": __version__, "status": result.status.value if result else None,
                "task": self._task, "answer": result.answer if result else "",
                "handoff": copy.deepcopy(result.handoff) if result else {},
                "steps": [step.serialize() for step in result.steps] if result else [],
                "verifications": copy.deepcopy(self.verifications),
                "protected_files": dict(self.protected_baseline),
                "messages": list(result.messages) if result else [],
                "component_config": self._redact(component_config)}

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
