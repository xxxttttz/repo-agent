from dataclasses import dataclass
from enum import Enum

from .environments.local import ExecutionStatus


class AgentStatus(str, Enum):
    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AgentStep:
    number: int
    content: str
    command: str | None
    output: str | None = None
    returncode: int | None = None
    execution_status: ExecutionStatus | None = None
    error: str | None = None
    output_truncated: bool = False
    completion_rejection: str | None = None
    submission: str | None = None

    def serialize(self) -> dict:
        return {
            "number": self.number,
            "content": self.content,
            "command": self.command,
            "output": self.output,
            "returncode": self.returncode,
            "execution_status": self.execution_status.value if self.execution_status else None,
            "error": self.error,
            "output_truncated": self.output_truncated,
            "completion_rejection": self.completion_rejection,
            "submission": self.submission,
        }

    @classmethod
    def deserialize(cls, data: dict) -> "AgentStep":
        status = data.get("execution_status")
        return cls(
            number=int(data["number"]),
            content=str(data.get("content", "")),
            command=data.get("command"),
            output=data.get("output"),
            returncode=data.get("returncode"),
            execution_status=ExecutionStatus(status) if status else None,
            error=data.get("error"),
            output_truncated=bool(data.get("output_truncated", False)),
            completion_rejection=data.get("completion_rejection"),
            submission=data.get("submission"),
        )


@dataclass(frozen=True, slots=True)
class AgentResult:
    status: AgentStatus
    answer: str
    steps: tuple[AgentStep, ...]
    messages: tuple[dict, ...] = ()

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def successful_commands(self) -> tuple[str, ...]:
        return tuple(
            step.command
            for step in self.steps
            if step.command is not None
            and step.execution_status is ExecutionStatus.SUCCESS
            and step.submission is None
        )
