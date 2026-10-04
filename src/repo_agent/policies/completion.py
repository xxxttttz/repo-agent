import re
import shlex
from dataclasses import dataclass
from typing import Protocol

from ..environments.local import LocalEnvironment


@dataclass(frozen=True, slots=True)
class CompletionContext:
    task: str
    environment: LocalEnvironment
    successful_commands: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompletionDecision:
    allowed: bool
    reason: str = ""
    required_commands: tuple[str, ...] = ()


class CompletionPolicy(Protocol):
    def evaluate(self, context: CompletionContext) -> CompletionDecision:
        """Decide whether the agent has enough evidence to finish."""


class FileEvidenceCompletionPolicy:
    """Require files named by the task to be inspected before completion."""

    _FILE_PATTERN = re.compile(
        r"[\w./-]+\."
        r"(?:py|cpp|cc|cxx|h|hpp|json|yaml|yml|toml|md|txt)"
    )
    _READ_COMMANDS = ("cat ", "head ", "tail ", "nl ", "sed ", "grep ")

    def evaluate(self, context: CompletionContext) -> CompletionDecision:
        targets = self._resolve_target_files(
            context.task,
            context.environment,
        )
        unread = []

        for requested_name, info in targets.items():
            status = info["status"]
            matches = info["matches"]

            if status == "missing":
                return CompletionDecision(
                    allowed=False,
                    reason=(
                        f"Target file '{requested_name}' could not be found "
                        "in the workspace."
                    ),
                )

            if status == "ambiguous":
                read_candidates = [
                    path
                    for path in matches
                    if self._was_file_read(
                        path,
                        context.successful_commands,
                    )
                ]

                if not read_candidates:
                    candidates = "\n".join(
                        f"- {path}" for path in matches
                    )
                    return CompletionDecision(
                        allowed=False,
                        reason=(
                            f"Target file '{requested_name}' is ambiguous.\n"
                            f"Candidates:\n{candidates}\n"
                            "Inspect the relevant candidate before finishing."
                        ),
                    )

                continue

            target_path = matches[0]
            if not self._was_file_read(
                target_path,
                context.successful_commands,
            ):
                unread.append(target_path)

        if unread:
            commands = tuple(f"cat {shlex.quote('./' + path if path.startswith('-') else path)}"
                             for path in dict.fromkeys(unread))
            return CompletionDecision(
                allowed=False,
                reason=("\n".join(f"You have not read the required target file '{path}' yet."
                                  for path in dict.fromkeys(unread))
                        + "\nRun these as separate actions (not a compound shell command):\n"
                        + "\n".join(commands)),
                required_commands=commands,
            )

        if not context.successful_commands:
            return CompletionDecision(False, "Run a successful non-submission command before finishing.")
        return CompletionDecision(allowed=True)

    def _extract_target_files(self, task: str) -> list[str]:
        files = self._FILE_PATTERN.findall(task)
        return list(dict.fromkeys(files))

    def _resolve_target_files(
        self,
        task: str,
        environment: LocalEnvironment,
    ) -> dict[str, dict]:
        resolved = {}

        for target in self._extract_target_files(task):
            normalized_target = target.removeprefix("./")
            filename = normalized_target.rsplit("/", 1)[-1]
            matches = environment.find_files(filename)
            exact_matches = [
                path for path in matches if path == normalized_target
            ]

            # Prefer an exact workspace-relative path such as README.md over
            # same-named files nested in caches or dependencies.
            if exact_matches:
                resolved[target] = {
                    "status": "resolved",
                    "matches": exact_matches,
                }
                continue

            if "/" in normalized_target:
                resolved[target] = {
                    "status": "missing",
                    "matches": matches,
                }
                continue

            if not matches:
                status = "missing"
            elif len(matches) == 1:
                status = "resolved"
            else:
                status = "ambiguous"

            resolved[target] = {
                "status": status,
                "matches": matches,
            }

        return resolved

    def _was_file_read(
        self,
        path: str,
        successful_commands: tuple[str, ...],
    ) -> bool:
        for command in successful_commands:
            # Fail closed for shell composition: a successful overall exit does
            # not prove that the read itself succeeded (e.g. `cat x || true`).
            if any(char in command for char in ("$", "`", "\n")):
                continue
            try:
                tokens = list(shlex.shlex(command, posix=True, punctuation_chars=True))
            except ValueError:
                continue
            if any(token and all(char in "();<>|&" for char in token) for token in tokens):
                continue
            if (tokens and tokens[0] + " " in self._READ_COMMANDS
                    and path in [token.removeprefix("./") for token in tokens[1:]]):
                return True

        return False
