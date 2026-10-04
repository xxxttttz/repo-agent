"""Read-only, exact-candidate exports with deliberately minimal saved evidence."""

from __future__ import annotations

import copy
import hashlib
import hmac
import re

from .worktree import WorktreeError

_COMMIT = re.compile(r"[0-9a-f]{40,64}")
_HASH = re.compile(r"[0-9a-f]{64}")
_EXPORT_STATUSES = {"awaiting_review", "approved", "rejected"}
_CHECK_STATES = {"not_configured", "passed", "failed", "cancelled", "error"}
_CHECK_STATUSES = {"success", "failed", "timed_out", "rejected"}


def _checks(evidence: dict | None) -> dict:
    if evidence is None:
        return {"state": "not_recorded", "checks": []}
    if not isinstance(evidence, dict):
        return {"state": "unknown", "checks": []}
    state = evidence.get("state")
    checks = evidence.get("checks")
    checks = checks if isinstance(checks, list) else []
    return {
        "state": state if isinstance(state, str) and state in _CHECK_STATES else "unknown",
        "checks": [{"number": index, "status": check.get("status") if isinstance(check.get("status"), str)
                    and check.get("status") in _CHECK_STATUSES else "unknown",
                    "returncode": check.get("returncode") if type(check.get("returncode")) is int else None,
                    "output_truncated": check.get("truncated") is True}
                   for index, check in enumerate((check if isinstance(check, dict) else {} for check in checks), 1)],
    }


def candidate_export(record: dict, *, commit: str, diff_sha256: str) -> dict:
    """Export the saved candidate, not a fresh workspace diff or approval."""
    candidate = record.get("candidate")
    if not isinstance(candidate, dict) or not candidate:
        raise WorktreeError("Task has no exportable candidate")
    if record.get("status") not in _EXPORT_STATUSES:
        raise WorktreeError("Candidate is not ready for export")
    if commit != candidate.get("commit") or diff_sha256 != candidate.get("diff_sha256"):
        raise WorktreeError("Export must reference the exact candidate commit and diff hash")
    patch = candidate.get("diff")
    paths = candidate.get("changed_paths")
    if (not isinstance(patch, str) or not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
            or not isinstance(candidate.get("base_commit"), str) or not _COMMIT.fullmatch(candidate["base_commit"])
            or not isinstance(diff_sha256, str) or not _HASH.fullmatch(diff_sha256) or not isinstance(paths, list)
            or any(not isinstance(path, str) for path in paths)):
        raise WorktreeError("Saved candidate metadata is invalid")
    content = patch.encode("utf-8")
    if len(content) > 1_000_000:
        raise WorktreeError("Candidate diff exceeds the 1 MB export limit")
    if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), diff_sha256):
        raise WorktreeError("Saved candidate diff does not match its hash")
    scope = record.get("scope_review")
    scope = scope if isinstance(scope, dict) else None
    review = record.get("review")
    review = review if isinstance(review, dict) else None
    scope_state = "not_recorded"
    if scope:
        scope_state = "failed" if scope.get("passed") is not True else (
            "passed" if scope.get("changed_paths") == paths else "inconsistent")
    report = {
        "schema_version": 1,
        "evidence_scope": "Saved candidate evidence only; export does not run checks, validate the current workspace, or approve a patch.",
        "task": {name: copy.deepcopy(record.get(name)) for name in (
            "id", "kind", "profile_id", "status", "created_at", "started_at", "finished_at", "delivery_mode")},
        "candidate": {name: copy.deepcopy(candidate[name]) for name in (
            "base_commit", "commit", "diff_sha256", "changed_paths")},
        "verification": {"baseline": _checks(record.get("baseline")),
                         "final": _checks(record.get("post_verification")),
                         "approval": _checks(record.get("approval_verification"))},
        "scope_review": {"state": scope_state},
        "review": {name: copy.deepcopy(review.get(name)) for name in (
            "decision", "at", "commit", "diff_sha256")} if review else None,
        "omitted": ["task text", "failure logs", "command text", "command outputs/errors", "model prose",
                    "trajectory", "profile configuration", "absolute workspace locations", "review reason"],
        "limitations": [
            "Recorded checks only establish their selected acceptance criteria; they do not prove all requirements.",
            "Source and candidate workspaces may have changed after these recorded checks.",
            "Patch contents and changed file names are not redacted; inspect them before sharing.",
            "The checksum detects changed bytes, not the trustworthiness of the service or patch.",
        ],
    }
    return {"patch": content, "report": report}
