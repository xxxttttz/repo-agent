"""Trusted server-side CI repair profiles and reproducibility checks."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from .environments import get_environment
from .environments.local import ExecutionStatus


@dataclass(frozen=True)
class RepairProfile:
    id: str
    workspace: str
    reproduce_commands: tuple[str, ...]
    verification_commands: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    protected_paths: tuple[str, ...] = ()
    title: str = "Python CI repair"
    provider: str = "mock"
    model: str | None = None
    max_steps: int = 20
    command_timeout: float = 30
    deadline_seconds: float = 600
    max_changed_files: int = 10

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.id):
            raise ValueError("Invalid repair profile id")
        if not isinstance(self.workspace, str) or not self.workspace.strip():
            raise ValueError("Repair workspace must be non-empty")
        if self.provider not in {"mock", "openrouter", "groq", "huggingface"}:
            raise ValueError("Invalid repair provider")
        for name in ("reproduce_commands", "verification_commands", "allowed_paths", "protected_paths"):
            value = getattr(self, name)
            maximum = 20 if name.endswith("commands") else 100
            if (not isinstance(value, (list, tuple)) or len(value) > maximum
                    or any(not isinstance(item, str) or not item.strip() for item in value)):
                raise ValueError(f"{name} must contain non-empty strings")
            object.__setattr__(self, name, tuple(value))
        if not self.reproduce_commands or not self.verification_commands or not self.allowed_paths:
            raise ValueError("Repair profiles require reproduction, verification and allowed paths")
        for path in (*self.allowed_paths, *self.protected_paths):
            if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts or "\\" in path:
                raise ValueError("Repair paths must be workspace-relative without '..'")
        if any(any(char in path for char in "*?[]") for path in self.protected_paths):
            raise ValueError("Protected paths are literal files, not glob patterns")
        if (type(self.max_steps) is not int or not 1 <= self.max_steps <= 100
                or type(self.max_changed_files) is not int or not 1 <= self.max_changed_files <= 100
                or type(self.command_timeout) not in {int, float} or not 0 < self.command_timeout <= 300
                or type(self.deadline_seconds) not in {int, float} or not 0 < self.deadline_seconds <= 3600):
            raise ValueError("Invalid repair budget")

    def serialize(self) -> dict:
        return asdict(self)

    def check_scope(self, paths: list[str]) -> str | None:
        if len(paths) > self.max_changed_files:
            return f"Changed-file budget exceeded: {len(paths)} > {self.max_changed_files}"
        unexpected = [path for path in paths if not any(fnmatch.fnmatchcase(path, pattern)
                                                       for pattern in self.allowed_paths)]
        if unexpected:
            return "Changes outside allowed paths: " + ", ".join(unexpected)
        return None


def load_profiles(path: str | None) -> dict[str, RepairProfile]:
    if not path:
        return {}
    import yaml

    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict) or set(data) != {"profiles"} or not isinstance(data["profiles"], list):
        raise ValueError("Repair configuration must contain a profiles list")
    profiles = {}
    for item in data["profiles"]:
        if not isinstance(item, dict):
            raise TypeError("Repair profile must be an object")
        profile = RepairProfile(**item)
        if profile.id in profiles:
            raise ValueError(f"Duplicate repair profile: {profile.id}")
        profiles[profile.id] = profile
    return profiles


def run_checks(workspace, commands, environment_config, *, timeout: float, cancelled) -> dict:
    """Execute trusted checks with the configured local/Docker executor."""
    if not commands:
        return {"state": "not_configured", "checks": []}
    config = {**environment_config, "cwd": str(workspace), "timeout": timeout}
    if config.get("environment_class", "local") == "local":
        config.setdefault("inherit_env", False)
    env = get_environment(config)
    checks = []
    for command in commands:
        if cancelled():
            return {"state": "cancelled", "checks": checks}
        result = env.execute(command)
        checks.append({"command": command, "status": result.status.value, "returncode": result.returncode,
                       "output": result.output, "error": result.error, "truncated": result.truncated})
        if cancelled():
            return {"state": "cancelled", "checks": checks}
        if (result.status not in {ExecutionStatus.SUCCESS, ExecutionStatus.FAILED}
                or result.returncode in {126, 127} or result.submission is not None):
            return {"state": "error", "checks": checks}
    return {"state": "failed" if any(check["status"] == "failed" for check in checks) else "passed",
            "checks": checks}
