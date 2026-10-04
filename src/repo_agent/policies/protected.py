"""Completion-time invariants for caller-selected workspace files."""

import hashlib
import os
import stat
from pathlib import Path


class ProtectedFiles:
    def __init__(self, cwd: str, paths: list[str] | tuple[str, ...]):
        if not isinstance(paths, (list, tuple)):
            raise ValueError("protected_paths must be a list of workspace-relative file paths")  # noqa: TRY004 - configuration errors use ValueError.
        self.root = Path(cwd).resolve()
        self.paths = []
        for value in paths:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("protected_paths must contain non-empty file paths")
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"Protected path must be workspace-relative: {value}")
            normalized = path.as_posix()
            if normalized not in self.paths:
                self.paths.append(normalized)

    def _fingerprint(self, relative: str) -> str:
        path = self.root / relative
        if not path.parent.resolve().is_relative_to(self.root):
            raise ValueError(f"Protected path escapes workspace: {relative}")
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return "missing"
        if stat.S_ISLNK(metadata.st_mode):
            return "symlink:" + os.readlink(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Protected path must be a file, not a directory or special file: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return f"sha256:{stat.S_IMODE(metadata.st_mode):o}:{digest.hexdigest()}"

    def capture(self) -> dict[str, str]:
        try:
            return {path: self._fingerprint(path) for path in self.paths}
        except (OSError, RuntimeError) as error:
            raise ValueError(f"Could not snapshot protected files: {error}") from error

    def check(self, baseline: dict[str, str]) -> str | None:
        try:
            current = self.capture()
        except ValueError as error:
            return str(error)
        changed = [path for path in self.paths if current[path] != baseline[path]]
        if changed:
            return ("Protected files changed: " + ", ".join(changed)
                    + ". Restore their original content/type/permissions before submitting. "
                    "Do not rewrite documentation or other protected files to summarize your work; "
                    "put the summary in your final answer.")
        return None
