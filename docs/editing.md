# Structured file edits

The agent can now submit a shell string or an edit object in the existing
`command` field. All three real provider adapters share the same schema and
strict validation. Existing shell actions and saved trajectories remain valid.

An exact replacement looks like this:

```json
{
  "content": "Convert the observed timeout expression to seconds.",
  "command": {
    "type": "edit",
    "mode": "replace",
    "path": "client.py",
    "old_text": "timeout_seconds = timeout_ms",
    "new_text": "timeout_seconds = timeout_ms / 1000"
  }
}
```

All five edit fields are required; extra fields are rejected. `old_text` must
match exactly once, including spaces and line endings. Include surrounding
context if a short substring appears more than once. The tool reports a failure
instead of silently doing nothing when the text is missing, ambiguous, or the
proposed change has no effect. Empty `old_text` matches only an empty file.

Use create mode for a new file:

```json
{
  "content": "Add a regression test.",
  "command": {
    "type": "edit",
    "mode": "create",
    "path": "tests/test_regression.py",
    "old_text": "",
    "new_text": "import unittest\n\nclass RegressionTests(unittest.TestCase):\n    def test_timeout(self):\n        from client import request_options\n        self.assertEqual(request_options({})['timeout_seconds'], 2.5)\n"
  }
}
```

Create refuses any existing target, including symlinks; it cannot overwrite a
file that appears during the edit. The parent directory must already exist.
Use a shell `mkdir -p` to create directories when needed. JSON represents file
contents directly, so shell quoting, `echo -e`, and shell expansion do not apply.

The executor enforces these checks:

- Paths must be workspace-relative and cannot contain `..`. Directory components
  and target files are opened without following symlinks. Directories and special
  files are not accepted as replacement targets.
- Files and edit text must be UTF-8, bounded to 2 MiB; the resulting file must
  also fit the limit. Bytes outside the replaced region are preserved, including
  line endings. Existing file permissions are preserved.
- Proposed `.py` and `.pyi` contents must parse before any target is changed.
  This catches syntax errors, not incorrect logic or missing imports. Refactors
  that temporarily break Python syntax must be expressed as a complete valid edit.
- Replacements use a sibling temporary file and atomic rename. The tool rereads
  the original before committing to detect concurrent changes. This is a stale
  data check, not a cross-process lock; unrelated writers can still race after
  that final check. Callers needing stronger coordination should use workspace
  locks or independent worktrees.

Successful observations include a diff and the resulting SHA-256, subject to
the normal output cap. Failure diagnostics are sent to the model so it can
reread the current file and correct the edit. The agent records the object as
the step's `command`, preserves it in provider history, and round-trips it through
trajectory save/resume. Edits never count as file-reading evidence and cannot
act as task submission markers.

The default prompt asks models to use structured edits for file changes and
shell for reads/tests. Shell writes remain available for compatibility; this
feature does not turn the local executor into an OS sandbox. `DefaultAgent`
rejects structured edits to files selected with `--protect` immediately. The
existing completion-time protected-file checks still cover shell changes.

Local execution runs the size-bounded editor in-process. Docker execution runs
the same stdlib-only editor source inside the existing restricted container,
using the normal command timeout and output capture. Docker images must contain
`python3`; the editor does not fall back to host-side file modification. The
container command transport is also subject to OS argument-size limits.

Tests cover exact/missing/ambiguous matches, syntax errors, Unicode, line endings,
file permissions, symlink escapes, write failures, concurrent changes, protocol
validation, provider history, protected files, save/resume, and the Docker command
path. The Docker command-path test simulates a container subprocess; it is not a
live Docker daemon test.
