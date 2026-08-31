"""Local shell execution environment."""

import os
import re
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from enum import Enum


class ExecutionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: ExecutionStatus
    output: str = ""
    returncode: int | None = None
    truncated: bool = False
    error: str | None = None
    submission: str | None = None


class DangerousCommandPolicy:
    """Reject a small set of commands that can destroy a workspace/system."""

    _PATTERNS = (
        re.compile(r"(?:^|[;&|\"'])\s*(?:sudo\s+)?(?:/bin/)?rm\b[^;&|\n]*?\s(?:/|~)(?=\s|$|[;&|\"'])"),
        re.compile(r"(?:^|[;&|\"'])\s*git\s+reset\s+--hard(?=\s|$|[;&|\"'])"),
    )

    @classmethod
    def reason(cls, command: str) -> str | None:
        if any(pattern.search(command) for pattern in cls._PATTERNS):
            return "Command rejected by safety policy: potentially destructive command."
        return None


class LocalEnvironment:
    def __init__(self, cwd: str, *, timeout: float = 30.0, max_output_size: int = 100_000):
        self.cwd = cwd
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_output_size < 0:
            raise ValueError("max_output_size must not be negative")
        self.timeout = timeout
        self.max_output_size = max_output_size

    def get_template_vars(self, **kwargs) -> dict:
        variables = {"cwd": self.cwd, "timeout": self.timeout, "max_output_size": self.max_output_size}
        variables.update(kwargs)
        return variables

    def execute(self, action: dict | str) -> ExecutionResult:
        if isinstance(action, dict):
            actions = action.get("extra", {}).get("actions", [])
            if not actions or not isinstance(actions[0], dict):
                return ExecutionResult(ExecutionStatus.REJECTED, error="Action contains no shell command.")
            command = actions[0].get("command")
        else:
            command = action
        if not isinstance(command, str):
            return ExecutionResult(ExecutionStatus.REJECTED, error="Action command must be a string.")
        rejection = DangerousCommandPolicy.reason(command)
        if rejection is not None:
            return ExecutionResult(ExecutionStatus.REJECTED, error=rejection)

        process = subprocess.Popen(command, shell=True, cwd=self.cwd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))
        captured = bytearray()
        truncated = False
        timed_out = False
        selector = selectors.DefaultSelector()
        assert process.stdout is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + self.timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    self._terminate(process)
                    break
                events = selector.select(remaining)
                if not events:
                    timed_out = True
                    self._terminate(process)
                    break
                for key, _ in events:
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = self.max_output_size - len(captured)
                    if available > 0:
                        captured.extend(chunk[:available])
                    if len(chunk) > max(available, 0):
                        truncated = True
            process.wait()
        finally:
            selector.close()
            process.stdout.close()
        output = bytes(captured).decode("utf-8", errors="replace")
        if timed_out:
            return ExecutionResult(ExecutionStatus.TIMED_OUT, output, process.returncode, truncated,
                                   f"Command timed out after {self.timeout:g} seconds.")
        status = ExecutionStatus.SUCCESS if process.returncode == 0 else ExecutionStatus.FAILED
        submission = None
        if status is ExecutionStatus.SUCCESS:
            lines = output.splitlines()
            marker = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
            if lines and lines[0].strip() == marker:
                submission = "\n".join(lines[1:]).strip()
        return ExecutionResult(status, output, process.returncode, truncated,
                               None if status is ExecutionStatus.SUCCESS else
                               f"Command exited with return code {process.returncode}.", submission)

    def serialize(self) -> dict:
        return {"class": f"{type(self).__module__}.{type(self).__name__}", "cwd": self.cwd,
                "timeout": self.timeout, "max_output_size": self.max_output_size}

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()

    def find_files(self, filename: str) -> list[str]:
        matches = []
        for root, _dirs, files in os.walk(self.cwd):
            if filename in files:
                matches.append(os.path.relpath(os.path.join(root, filename), self.cwd))
        return matches
