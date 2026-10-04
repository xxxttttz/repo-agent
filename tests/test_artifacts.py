import copy
import hashlib
import json

import pytest

from repo_agent.artifacts import candidate_export
from repo_agent.worktree import WorktreeError


def record(status="awaiting_review", patch="diff --git a/app.py b/app.py\n+# 中文修复\n"):
    return {
        "id": "a" * 32, "kind": "ci_repair", "profile_id": "python", "status": status,
        "task": "PRIVATE_TASK_TEXT", "failure_log": "PRIVATE_LOG",
        "source_workspace": "/private/source", "worktree_path": "/private/worktree",
        "repair_profile": {"model": "PRIVATE_PROFILE", "verification_commands": ["PRIVATE_COMMAND"]},
        "result": {"answer": "PRIVATE_MODEL_PROSE", "messages": ["PRIVATE_TRAJECTORY"]},
        "candidate": {"commit": "c" * 40, "base_commit": "b" * 40, "changed_paths": ["app.py"],
                      "diff": patch, "diff_sha256": hashlib.sha256(patch.encode()).hexdigest()},
        "baseline": {"state": "failed", "checks": [{"command": "PRIVATE_COMMAND", "status": "failed",
                     "returncode": 1, "output": "PRIVATE_OUTPUT", "error": "PRIVATE_ERROR", "truncated": True}]},
        "post_verification": {"state": "passed", "checks": [{"command": "PRIVATE_COMMAND", "status": "success",
                              "returncode": 0, "output": "PRIVATE_OUTPUT"}]},
        "scope_review": {"passed": True, "changed_paths": ["app.py"], "error": "PRIVATE_ERROR"},
        "review": {"decision": status, "at": "2026-10-04T10:00:00+00:00", "reason": "PRIVATE_REVIEW_REASON",
                   "commit": "c" * 40, "diff_sha256": hashlib.sha256(patch.encode()).hexdigest()} if status != "awaiting_review" else None,
    }


def export(saved):
    return candidate_export(saved, commit=saved["candidate"]["commit"], diff_sha256=saved["candidate"]["diff_sha256"])


@pytest.mark.parametrize("status", ["awaiting_review", "approved", "rejected"])
def test_exact_candidate_export_is_read_only_and_omits_private_fields(status):
    saved = record(status)
    before = copy.deepcopy(saved)
    artifact = export(saved)
    assert saved == before
    assert artifact["patch"] == saved["candidate"]["diff"].encode("utf-8")
    assert hashlib.sha256(artifact["patch"]).hexdigest() == saved["candidate"]["diff_sha256"]
    report = artifact["report"]
    assert report["task"]["status"] == status
    assert report["candidate"] == {key: value for key, value in saved["candidate"].items() if key != "diff"}
    assert report["verification"]["baseline"]["state"] == "failed"
    assert report["verification"]["final"]["state"] == "passed"
    assert report["verification"]["approval"]["state"] == "not_recorded"
    assert report["verification"]["baseline"]["checks"] == [
        {"number": 1, "status": "failed", "returncode": 1, "output_truncated": True},
    ]
    assert report["scope_review"]["state"] == "passed"
    assert "PRIVATE_" not in json.dumps(report)
    assert "/private/" not in json.dumps(report)
    assert "diff" not in report["candidate"]
    assert "does not run checks" in report["evidence_scope"]


@pytest.mark.parametrize("change", ["commit", "hash"])
def test_export_refuses_different_review_identity(change):
    saved = record()
    commit, checksum = saved["candidate"]["commit"], saved["candidate"]["diff_sha256"]
    with pytest.raises(WorktreeError, match="exact candidate"):
        candidate_export(saved, commit="0" * 40 if change == "commit" else commit,
                         diff_sha256="0" * 64 if change == "hash" else checksum)


@pytest.mark.parametrize("status", ["queued", "running", "error", "cancelled", "completed"])
def test_export_requires_a_finalized_review_candidate(status):
    with pytest.raises(WorktreeError, match="not ready"):
        export(record(status))


def test_export_rejects_corrupted_saved_patch():
    saved = record()
    saved["candidate"]["diff"] += "changed after recording"
    with pytest.raises(WorktreeError, match="does not match"):
        export(saved)


@pytest.mark.parametrize("field, value", [
    ("base_commit", "bad"), ("diff", None), ("changed_paths", "app.py"),
    ("changed_paths", [123]), ("diff_sha256", "bad"), ("commit", "bad"),
])
def test_export_rejects_invalid_candidate_metadata(field, value):
    saved = record()
    saved["candidate"][field] = value
    with pytest.raises(WorktreeError, match="invalid"):
        export(saved)


def test_export_rejects_oversized_diff_instead_of_truncating():
    with pytest.raises(WorktreeError, match="1 MB"):
        export(record(patch="x" * 1_000_001))


def test_no_candidate_cannot_be_exported():
    saved = record()
    saved["candidate"] = None
    with pytest.raises(WorktreeError, match="no exportable"):
        candidate_export(saved, commit="c" * 40, diff_sha256="d" * 64)


def test_report_does_not_claim_an_unrecorded_check_passed():
    saved = record()
    saved["baseline"] = saved["post_verification"] = saved["scope_review"] = None
    saved["approval_verification"] = {"state": "not_configured", "checks": []}
    report = export(saved)["report"]
    assert report["verification"]["baseline"]["state"] == "not_recorded"
    assert report["verification"]["final"]["state"] == "not_recorded"
    assert report["verification"]["approval"]["state"] == "not_configured"
    assert report["scope_review"]["state"] == "not_recorded"


def test_report_marks_scope_evidence_for_different_paths_inconsistent():
    saved = record()
    saved["scope_review"]["changed_paths"] = ["another.py"]
    assert export(saved)["report"]["scope_review"]["state"] == "inconsistent"


def test_patch_itself_is_not_redacted_or_modified():
    saved = record(patch="+password = 'PRIVATE_SECRET'\n")
    artifact = export(saved)
    assert b"PRIVATE_SECRET" in artifact["patch"]
    assert "PRIVATE_SECRET" not in json.dumps(artifact["report"])


def test_malformed_check_evidence_is_not_an_accepted_check():
    saved = record()
    saved["post_verification"] = {"state": {"unknown": "PRIVATE_STATE"}, "checks": [
        {"status": "PRIVATE_STATUS", "returncode": True, "output": "PRIVATE_OUTPUT"}, None,
    ]}
    evidence = export(saved)["report"]["verification"]["final"]
    assert evidence["state"] == "unknown"
    assert all(check["status"] == "unknown" and check["returncode"] is None for check in evidence["checks"])
    assert "PRIVATE_" not in json.dumps(evidence)
