"""Exact text edits, usable in-process or as a standalone Docker script.

Keep this module stdlib-only and free of package-relative imports: the Docker
executor runs its source inside the configured container.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

MAX_FILE_BYTES = 2 * 1024 * 1024
EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["edit"]},
        "mode": {"type": "string", "enum": ["replace", "create"]},
        "path": {"type": "string"},
        "old_text": {"type": "string"},
        "new_text": {"type": "string"},
    },
    "required": ["type", "mode", "path", "old_text", "new_text"],
    "additionalProperties": False,
}


class EditError(ValueError):
    """A rejected or unsuccessful edit; no target file was written."""


def validate_edit(edit: dict) -> None:
    if set(edit) != set(EDIT_SCHEMA["required"]):
        raise EditError("Edit requires exactly type, mode, path, old_text, new_text.")
    if any(not isinstance(value, str) for value in edit.values()):
        raise EditError("All edit fields must be strings.")
    if edit["type"] != "edit" or edit["mode"] not in {"replace", "create"}:
        raise EditError("Edit type must be 'edit'; mode must be 'replace' or 'create'.")
    path = Path(edit["path"])
    if not edit["path"].strip() or not path.parts or path.is_absolute() or ".." in path.parts or "\x00" in edit["path"]:
        raise EditError("Edit path must be a non-empty workspace-relative file path without '..'.")
    if edit["mode"] == "create" and edit["old_text"]:
        raise EditError("Create mode requires empty old_text and a nonexistent target file.")
    try:
        if any(len(edit[key].encode("utf-8")) > MAX_FILE_BYTES for key in ("old_text", "new_text")):
            raise EditError("Edit exceeds the 2 MiB file limit.")
    except UnicodeError as error:
        raise EditError("Edit text must be valid UTF-8.") from error


@contextmanager
def _parent_descriptor(cwd: str, relative: str):
    """Traverse existing directories without following symlinks, then pin the parent."""
    parts = Path(relative).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(str(Path(cwd).resolve()), flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _read_file(parent: int, name: str) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise EditError("Edit target must be a regular file.")
        if metadata.st_size > MAX_FILE_BYTES:
            raise EditError("Edit target exceeds the 2 MiB file limit.")
        data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise EditError("Edit target exceeds the 2 MiB file limit.")
    return data, metadata


def _identity(metadata: os.stat_result) -> tuple:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_mtime_ns, metadata.st_ctime_ns


def execute_edit(edit: dict, cwd: str) -> str:
    """Validate first, write a sibling temporary file, then atomically install it."""
    validate_edit(edit)
    try:
        with _parent_descriptor(cwd, edit["path"]) as (parent, name):
            old_data, metadata = b"", None
            if edit["mode"] == "create":
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise EditError("Create refused: target already exists; use replace with observed old_text.")
                candidate = edit["new_text"]
            else:
                old_data, metadata = _read_file(parent, name)
                old_source = old_data.decode("utf-8")
                old_text = edit["old_text"]
                if not old_text and old_source:
                    raise EditError("Empty old_text only matches an empty file.")
                first = old_source.find(old_text)
                if first < 0:
                    raise EditError("old_text did not match; read the current file and retry. No changes made.")
                if old_text and old_source.find(old_text, first + 1) >= 0:
                    raise EditError("old_text is ambiguous; include more context for exactly one match.")
                candidate = old_source[:first] + edit["new_text"] + old_source[first + len(old_text):]
                if candidate == old_source:
                    raise EditError("Edit makes no change.")
            new_data = candidate.encode("utf-8")
            if len(new_data) > MAX_FILE_BYTES:
                raise EditError("Result exceeds the 2 MiB file limit.")
            if Path(name).suffix in {".py", ".pyi"}:
                try:
                    ast.parse(candidate, filename=edit["path"])
                except (SyntaxError, ValueError, RecursionError) as error:
                    raise EditError(f"Python syntax check failed: {error}. No changes made.") from error
            diff_lines = difflib.unified_diff(
                old_data.decode("utf-8").splitlines(keepends=True), candidate.splitlines(keepends=True),
                fromfile=edit["path"], tofile=edit["path"],
            )
            preview = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                              for line in diff_lines)
            mode = stat.S_IMODE(metadata.st_mode) if metadata else 0o644
            temporary = f".repo-agent-edit-{uuid4().hex}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(new_data)
                    handle.flush()
                    os.fchmod(handle.fileno(), mode)
                    os.fsync(handle.fileno())
                if metadata is not None:
                    current, current_metadata = _read_file(parent, name)
                    if current != old_data or _identity(current_metadata) != _identity(metadata):
                        raise EditError("Target changed during editing; read it again before retrying.")
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                else:
                    # link is exclusive: never overwrite a file created concurrently.
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
            return (f"Edited {edit['path']} ({edit['mode']}).\n"
                    f"SHA-256: {hashlib.sha256(new_data).hexdigest()}\n{preview}")
    except (OSError, UnicodeError) as error:
        raise EditError(f"Edit failed: {error}. Read the current file before retrying.") from error


def main() -> int:
    try:
        payload = json.loads(sys.argv[1])
        if not isinstance(payload, dict):
            raise EditError("Edit payload must be an object.")
        print(execute_edit(payload, os.getcwd()))
        return 0
    except (EditError, json.JSONDecodeError, IndexError) as error:
        print(f"Edit failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
