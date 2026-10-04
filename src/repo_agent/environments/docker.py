"""One-shot Docker execution environment with restrictive defaults."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from ..tools.edit import EditError, validate_edit
from .local import LocalEnvironment


class DockerEnvironment(LocalEnvironment):
    """Run every shell action in a fresh container over one mounted workspace."""

    def __init__(
        self,
        cwd: str,
        *,
        image: str = "python:3.13-slim",
        docker_binary: str = "docker",
        network: str = "none",
        memory: str = "1g",
        cpus: float = 1.0,
        pids_limit: int = 256,
        read_only: bool = True,
        pull: str = "never",
        env_allowlist: list[str] | tuple[str, ...] = (),
        timeout: float = 30.0,
        max_output_size: int = 100_000,
    ):
        if cpus <= 0:
            raise ValueError("cpus must be greater than zero")
        if pids_limit < 1:
            raise ValueError("pids_limit must be at least 1")
        if pull not in {"always", "missing", "never"}:
            raise ValueError("pull must be one of: always, missing, never")
        super().__init__(
            cwd,
            timeout=timeout,
            max_output_size=max_output_size,
            inherit_env=True,
        )
        self.image = image
        self.docker_binary = docker_binary
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.read_only = read_only
        self.pull = pull
        self.container_env_allowlist = tuple(env_allowlist)
        self._active_container_name: str | None = None

    def _start_process(self, command: str) -> subprocess.Popen:
        workspace = str(Path(self.cwd).expanduser().resolve())
        container_name = f"repo-agent-{uuid4().hex}"
        self._active_container_name = container_name
        arguments = [
            self.docker_binary,
            "run",
            "--rm",
            "--name",
            container_name,
            f"--pull={self.pull}",
            "--network",
            self.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids_limit),
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--workdir",
            "/workspace",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m",
        ]
        if self.read_only:
            arguments.append("--read-only")
        if os.name != "nt" and hasattr(os, "getuid"):
            arguments.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        for name in self.container_env_allowlist:
            if name in os.environ:
                arguments.extend(["--env", name])
        arguments.extend([self.image, "/bin/sh", "-lc", command])
        return subprocess.Popen(
            arguments,
            cwd=self.cwd,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )

    def _execute_edit(self, command: dict):
        # Use the same bounded container as shell actions. No host-side edit.
        from .local import ExecutionResult, ExecutionStatus

        try:
            validate_edit(command)
        except EditError as error:
            return ExecutionResult(ExecutionStatus.REJECTED, error=str(error))
        source = files("repo_agent.tools").joinpath("edit.py").read_text(encoding="utf-8")
        shell_command = shlex.join(["python3", "-c", source, json.dumps(command, ensure_ascii=False)])
        return super().execute(shell_command)

    def _terminate(self, process: subprocess.Popen) -> None:
        LocalEnvironment._terminate(process)
        if self._active_container_name is None:
            return
        try:
            subprocess.run(
                [self.docker_binary, "rm", "--force", self._active_container_name],
                check=False,
                env=os.environ.copy(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def get_template_vars(self, **kwargs) -> dict:
        variables = super().get_template_vars(**kwargs)
        variables["cwd"] = "/workspace"
        return variables

    def serialize(self) -> dict:
        return {
            "class": f"{type(self).__module__}.{type(self).__name__}",
            "cwd": self.cwd,
            "timeout": self.timeout,
            "max_output_size": self.max_output_size,
            "image": self.image,
            "docker_binary": self.docker_binary,
            "network": self.network,
            "memory": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "read_only": self.read_only,
            "pull": self.pull,
            "env_allowlist": list(self.container_env_allowlist),
        }
