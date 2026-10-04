import asyncio
import hashlib
import shlex
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from repo_agent.api import (
    RepairRequest,
    ReviewRequest,
    TaskManager,
    TaskRequest,
    create_app,
)
from repo_agent.models import MessageModel
from repo_agent.repairs import RepairProfile, load_profiles, run_checks
from repo_agent.worktree import WorktreeError

CHECK = shlex.join([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"])
BAD = "def double(value):\n    return value\n"
GOOD = "def double(value):\n    return value * 2\n"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, message="test"):
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", message)


def repository(root, *, broken=True):
    repo = root / "project"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (repo / "README.md").write_text("Keep this documentation unchanged.\n")
    (repo / "app.py").write_text(BAD if broken else GOOD)
    (repo / "tests").mkdir()
    (repo / "tests/test_app.py").write_text(
        "import unittest\nfrom app import double\nclass Tests(unittest.TestCase):\n"
        "    def test_double(self):\n        self.assertEqual(double(3), 6)\n")
    commit(repo, "baseline")
    return repo


def profile(**overrides):
    return replace(RepairProfile(id="demo", workspace="project", reproduce_commands=(CHECK,),
                                 verification_commands=(CHECK, "git diff --check"),
                                 allowed_paths=("app.py", "tests/test_regression*.py"),
                                 protected_paths=("README.md", "tests/test_app.py")), **overrides)


class RepairModel(MessageModel):
    def __init__(self):
        super().__init__()
        self.commands = iter([
            "cat app.py", "cat README.md",
            {"type": "edit", "mode": "replace", "path": "app.py", "old_text": BAD, "new_text": GOOD},
            {"type": "edit", "mode": "create", "path": "tests/test_regression_double.py", "old_text": "",
             "new_text": "import unittest\nfrom app import double\nclass Regression(unittest.TestCase):\n"
                         "    def test_zero(self):\n        self.assertEqual(double(0), 0)\n"},
            CHECK,
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
        ])

    def query(self, messages):
        return self.format_message("修复 app.py 的 double，新增回归测试。已观察测试通过；提交检查由执行器确认。",
                                   [{"command": next(self.commands)}])


def wait(manager, identifier):
    for _ in range(500):
        record = manager.get(identifier)
        if record["status"] not in {"queued", "running"}:
            return record
        time.sleep(.01)
    raise AssertionError("Repair did not finish")


def submit(manager):
    return wait(manager, manager.submit_repair(RepairRequest(
        profile="demo", task="修复 app.py 中的 double 并补回归测试", failure_log="AssertionError: 3 != 6"))["id"])


def decision(record):
    return ReviewRequest(commit=record["candidate"]["commit"], diff_sha256=record["candidate"]["diff_sha256"])


@pytest.fixture
def repair(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: RepairModel())
    manager = TaskManager(tmp_path, redis_url=None, worktree_root=tmp_path / "managed", repair_profiles={"demo": profile()})
    record = submit(manager)
    yield manager, repo, record
    manager.close()


def test_end_to_end_reproduction_candidate_and_manual_approval(repair):
    manager, repo, record = repair
    assert record["status"] == "awaiting_review", record
    assert record["baseline"]["state"] == "failed"
    assert record["post_verification"]["state"] == "passed"
    assert record["scope_review"]["passed"]
    assert record["candidate"]["changed_paths"] == ["app.py", "tests/test_regression_double.py"]
    assert "+    return value * 2" in record["candidate"]["diff"]
    assert record["worktree_cleaned"] is False
    assert (repo / "app.py").read_text() == BAD
    assert git(repo, "rev-parse", "HEAD") == record["candidate"]["base_commit"]
    assert manager.get_session(record["session_id"], "project")["turns"] == []
    approved = manager.review(record["id"], decision(record), approve=True)
    assert approved["status"] == "approved"
    assert approved["approval_verification"]["state"] == "passed"
    assert approved["worktree_merged"] is True
    assert git(repo, "rev-parse", "HEAD") == record["candidate"]["commit"]
    assert (repo / "app.py").read_text() == GOOD
    assert Path(record["worktree_path"]).exists()
    assert len(manager.get_session(record["session_id"], "project")["turns"]) == 1
    assert manager.review(record["id"], decision(record), approve=True)["status"] == "approved"


def test_rejection_preserves_candidate_and_never_changes_source(repair):
    manager, repo, record = repair
    rejected = manager.review(record["id"], decision(record), approve=False)
    assert rejected["status"] == "rejected"
    assert (repo / "app.py").read_text() == BAD
    assert Path(record["worktree_path"]).exists()
    assert manager.review(record["id"], decision(record), approve=False)["status"] == "rejected"
    with pytest.raises(WorktreeError, match="not awaiting"):
        manager.review(record["id"], decision(record), approve=True)


def test_export_uses_saved_candidate_and_never_reads_or_changes_current_workspaces(repair, monkeypatch):
    manager, repo, record = repair
    worktree = Path(record["worktree_path"])
    (repo / "app.py").write_text("# source changed externally\n")
    (worktree / "app.py").write_text("# candidate changed externally\n")
    monkeypatch.setattr(manager.worktree_manager, "candidate", lambda *args: pytest.fail("Export must not query live Git"))
    before = manager.get(record["id"])
    artifact = manager.export_candidate(record["id"], decision(record))
    assert artifact["patch"] == record["candidate"]["diff"].encode()
    assert artifact["report"]["task"]["status"] == "awaiting_review"
    assert (repo / "app.py").read_text() == "# source changed externally\n"
    assert (worktree / "app.py").read_text() == "# candidate changed externally\n"
    assert manager.get(record["id"]) == before
    assert manager.get_session(record["session_id"], "project")["turns"] == []


@pytest.mark.parametrize("approve", [True, False])
def test_export_after_a_decision_retains_decision_without_changing_it(repair, approve):
    manager, _repo, record = repair
    reviewed = manager.review(record["id"], decision(record), approve=approve)
    artifact = manager.export_candidate(record["id"], decision(record))
    assert artifact["report"]["task"]["status"] == reviewed["status"]
    assert artifact["report"]["review"]["decision"] == reviewed["status"]
    assert artifact["report"]["verification"]["approval"]["state"] == ("passed" if approve else "not_recorded")
    assert manager.get(record["id"]) == reviewed


@pytest.mark.parametrize("change", ["source_commit", "source_branch", "source_dirty", "candidate_dirty", "candidate_commit", "wrong_hash"])
def test_review_refuses_stale_or_modified_inputs(repair, change):
    manager, repo, record = repair
    request = decision(record)
    worktree = Path(record["worktree_path"])
    if change == "source_commit":
        (repo / "extra.txt").write_text("external commit\n")
        commit(repo)
    elif change == "source_branch":
        git(repo, "switch", "-c", "external-branch")
    elif change == "source_dirty":
        (repo / "extra.txt").write_text("user work\n")
    elif change == "candidate_dirty":
        (worktree / "app.py").write_text("# external edit\n")
    elif change == "candidate_commit":
        (worktree / "extra.txt").write_text("external candidate commit\n")
        commit(worktree)
    else:
        request.diff_sha256 = "0" * 64
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(WorktreeError):
        manager.review(record["id"], request, approve=True)
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / "app.py").read_text() == BAD
    assert manager.get(record["id"])["status"] == "awaiting_review"


def test_approval_reruns_checks_and_blocks_transient_failure(repair, monkeypatch):
    manager, repo, record = repair
    monkeypatch.setattr("repo_agent.api.run_checks", lambda *args, **kwargs: {"state": "failed", "checks": []})
    with pytest.raises(WorktreeError, match="Approval verification"):
        manager.review(record["id"], decision(record), approve=True)
    assert manager.get(record["id"])["approval_verification"]["state"] == "failed"
    assert (repo / "app.py").read_text() == BAD


def test_approval_check_cannot_modify_the_reviewed_workspace(repair, monkeypatch):
    manager, repo, record = repair

    def modifying_check(workspace, *args, **kwargs):
        (workspace / "app.py").write_text("# changed during verification\n")
        return {"state": "passed", "checks": []}

    monkeypatch.setattr("repo_agent.api.run_checks", modifying_check)
    with pytest.raises(WorktreeError, match="workspace changed"):
        manager.review(record["id"], decision(record), approve=True)
    assert (repo / "app.py").read_text() == BAD
    assert manager.get(record["id"])["status"] == "awaiting_review"


def test_review_survives_manager_restart_with_saved_records(repair):
    manager, repo, record = repair
    store = manager.task_store
    manager.close()
    restarted = TaskManager(repo.parent, redis_url=None, task_store=store, worktree_root=manager.worktree_manager.root)
    try:
        assert restarted.review(record["id"], decision(record), approve=True)["status"] == "approved"
    finally:
        restarted.close()


def test_passing_baseline_does_not_call_model(tmp_path, monkeypatch):
    repository(tmp_path, broken=False)
    monkeypatch.setattr("repo_agent.api.execute_task", lambda *args, **kwargs: pytest.fail("Model must not run"))
    manager = TaskManager(tmp_path, redis_url=None, worktree_root=tmp_path / "managed", repair_profiles={"demo": profile()})
    try:
        record = submit(manager)
        assert record["status"] == "not_reproduced"
        assert record["baseline"]["state"] == "passed"
        assert record["candidate"] is None
    finally:
        manager.close()


@pytest.mark.parametrize("bad_change", ["outside", "protected", "too_many", "no_fix"])
def test_repair_cannot_be_accepted_by_claiming_completion(tmp_path, monkeypatch, bad_change):
    repo = repository(tmp_path)
    configured = profile(max_changed_files=1) if bad_change == "too_many" else profile()

    def execute(spec, **kwargs):
        if bad_change != "no_fix":
            (spec.workspace / "app.py").write_text(GOOD)
        if bad_change == "outside":
            (spec.workspace / "outside.txt").write_text("unrelated\n")
        elif bad_change == "protected":
            (spec.workspace / "README.md").write_text("overwritten\n")
        elif bad_change == "too_many":
            (spec.workspace / "tests/test_regression_extra.py").write_text("# extra\n")
        return SimpleNamespace(trajectory={"status": "completed", "answer": "All done", "steps": [], "messages": []}, index={})

    monkeypatch.setattr("repo_agent.api.execute_task", execute)
    manager = TaskManager(tmp_path, redis_url=None, worktree_root=tmp_path / "managed", repair_profiles={"demo": configured})
    try:
        record = submit(manager)
        assert record["status"] == "error", record
        assert record["candidate"] is None
        assert record["result"]["answer"] == "All done"
        assert (repo / "app.py").read_text() == BAD
        assert Path(record["worktree_path"]).exists()
    finally:
        manager.close()


@pytest.mark.parametrize("command", ["nonexistent-repair-executable", "sleep 2", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"])
def test_unusable_baseline_is_not_classified_as_reproduced(tmp_path, command):
    result = run_checks(tmp_path, [command], {}, timeout=.05, cancelled=lambda: False)
    assert result["state"] == "error"


def test_profiles_are_trusted_config_and_duplicate_ids_fail(tmp_path):
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump({"profiles": [profile().serialize()]}))
    assert load_profiles(str(path))["demo"] == profile()
    path.write_text(yaml.safe_dump({"profiles": [profile().serialize(), profile().serialize()]}))
    with pytest.raises(ValueError, match="Duplicate"):
        load_profiles(str(path))
    assert load_profiles(None) == {}


@pytest.mark.parametrize("overrides", [
    {"allowed_paths": ("../outside.py",)}, {"allowed_paths": ("/tmp/outside.py",)},
    {"protected_paths": ("tests/*.py",)}, {"max_steps": True}, {"command_timeout": "30"},
    {"deadline_seconds": float("nan")}, {"max_changed_files": 0},
])
def test_profiles_reject_invalid_scope_and_budgets(overrides):
    with pytest.raises(ValueError):
        profile(**overrides)


def test_empty_checks_do_not_claim_success(tmp_path):
    assert run_checks(tmp_path, [], {}, timeout=30, cancelled=lambda: False) == {
        "state": "not_configured", "checks": [],
    }


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_controller_does_not_publish_candidate_after_execution_stops(tmp_path, monkeypatch, stop):
    repo = repository(tmp_path)
    clock = [0.0]
    monkeypatch.setattr("repo_agent.api.time", SimpleNamespace(monotonic=lambda: clock[0]))
    manager = TaskManager(tmp_path, redis_url=None, worktree_root=tmp_path / "managed", repair_profiles={"demo": profile()})

    def execute(spec, **kwargs):
        (spec.workspace / "app.py").write_text(GOOD)
        if stop == "cancel":
            identifier = spec.workspace.name
            manager.cancel(identifier)
        else:
            clock[0] = 1000.0
        return SimpleNamespace(trajectory={"status": "completed", "answer": "done", "steps": []}, index={})

    monkeypatch.setattr("repo_agent.api.execute_task", execute)
    try:
        record = submit(manager)
        assert record["status"] == ("cancelled" if stop == "cancel" else "error"), record
        assert record["candidate"] is None
        assert (repo / "app.py").read_text() == BAD
        assert Path(record["worktree_path"]).exists()
    finally:
        manager.close()


def test_general_git_tasks_default_to_review_without_inventing_checks(tmp_path, monkeypatch):
    repo = repository(tmp_path)

    def execute(spec, **kwargs):
        (spec.workspace / "app.py").write_text(GOOD)
        return SimpleNamespace(trajectory={"status": "completed", "answer": "done", "steps": []}, index={})

    monkeypatch.setattr("repo_agent.api.execute_task", execute)
    manager = TaskManager(tmp_path, redis_url=None, worktree_root=tmp_path / "managed")
    try:
        accepted = manager.submit(TaskRequest(task="fix", workspace="project"))
        record = wait(manager, accepted["id"])
        assert record["status"] == "awaiting_review"
        assert (repo / "app.py").read_text() == BAD
        approved = manager.review(record["id"], decision(record), approve=True)
        assert approved["approval_verification"]["state"] == "not_configured"
        assert (repo / "app.py").read_text() == GOOD
    finally:
        manager.close()


def test_repair_rejects_unknown_profile_and_non_git_workspace(tmp_path):
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": profile(workspace=".")})
    try:
        with pytest.raises(ValueError, match="Unknown"):
            manager.submit_repair(RepairRequest(profile="unknown", task="fix", failure_log="failure"))
        with pytest.raises(ValueError, match="Git repository"):
            manager.submit_repair(RepairRequest(profile="demo", task="fix", failure_log="failure"))
    finally:
        manager.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_console_and_authenticated_repair_api(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    config = tmp_path / "profiles.yaml"
    config.write_text(yaml.safe_dump({"profiles": [profile().serialize()]}))
    monkeypatch.setenv("REPO_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("REPO_AGENT_REPAIR_PROFILES", str(config))
    monkeypatch.setenv("REPO_AGENT_API_TOKEN", "test-access")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: RepairModel())
    app = create_app()
    app.state.task_manager.worktree_manager.root = tmp_path / "managed"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/")).status_code == 200
            assert "CI 修复工单台" in (await client.get("/")).text
            assert (await client.get("/repair-profiles")).status_code == 401
            assert (await client.get("/tasks")).status_code == 401
            assert (await client.get("/tasks/unknown/candidate.patch")).status_code == 401
            assert (await client.get("/tasks/unknown/candidate-report.json")).status_code == 401
            assert (await client.post("/tasks", json={"task": "bypass"})).status_code == 401
            client.headers["Authorization"] = "Bearer test-access"
            assert (await client.post("/tasks", json={"task": "bypass profile"})).status_code == 403
            assert (await client.get("/repair-profiles")).json()["profiles"][0]["id"] == "demo"
            bad = await client.post("/repairs", json={"profile": "demo", "task": "fix", "failure_log": "failure",
                                                     "verification_commands": ["true"]})
            assert bad.status_code == 422
            accepted = await client.post("/repairs", json={"profile": "demo", "task": "修复 app.py", "failure_log": "3 != 6"})
            assert accepted.status_code == 202
            identifier = accepted.json()["id"]
            for _ in range(500):
                record = (await client.get(f"/tasks/{identifier}")).json()
                if record["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(.01)
            assert record["status"] == "awaiting_review", record
            history = (await client.get("/tasks", params={"status": "awaiting_review", "kind": "ci_repair"})).json()
            assert [row["id"] for row in history["tasks"]] == [identifier]
            assert history["tasks"][0]["profile_id"] == "demo"
            assert "candidate" not in history["tasks"][0]
            candidate = (await client.get(f"/tasks/{identifier}/candidate")).json()
            assert candidate == record["candidate"]
            binding = {"commit": candidate["commit"], "diff_sha256": candidate["diff_sha256"]}
            for endpoint in ["candidate.patch", "candidate-report.json"]:
                assert (await client.get(f"/tasks/unknown/{endpoint}", params=binding)).status_code == 404
                assert (await client.get(f"/tasks/{identifier}/{endpoint}")).status_code == 422
                invalid = {**binding, "diff_sha256": "0" * 64}
                assert (await client.get(f"/tasks/{identifier}/{endpoint}", params=invalid)).status_code == 409
                invalid = {**binding, "commit": candidate["commit"] + '\n"bad'}
                assert (await client.get(f"/tasks/{identifier}/{endpoint}", params=invalid)).status_code == 422
            patch = await client.get(f"/tasks/{identifier}/candidate.patch", params=binding)
            assert patch.status_code == 200
            assert patch.content == candidate["diff"].encode("utf-8")
            assert hashlib.sha256(patch.content).hexdigest() == candidate["diff_sha256"]
            assert patch.headers["content-type"] == "application/octet-stream"
            assert patch.headers["cache-control"] == "no-store"
            assert patch.headers["x-content-type-options"] == "nosniff"
            assert patch.headers["x-repo-agent-commit"] == candidate["commit"]
            assert patch.headers["x-repo-agent-diff-sha256"] == candidate["diff_sha256"]
            assert patch.headers["content-disposition"] == f'attachment; filename="repo-agent-{candidate["commit"][:12]}.patch"'
            exported = tmp_path / "download.patch"
            exported.write_bytes(patch.content)
            git(repo, "apply", "--check", str(exported))
            report = await client.get(f"/tasks/{identifier}/candidate-report.json", params=binding)
            assert report.status_code == 200
            assert report.json()["verification"]["final"]["state"] == "passed"
            assert report.json()["verification"]["approval"]["state"] == "not_recorded"
            assert report.json()["review"] is None
            assert "failure_log" not in report.json()
            assert "3 != 6" not in report.text
            assert str(repo) not in report.text
            manager_record = app.state.task_manager.get(identifier)
            assert manager_record is not None
            assert manager_record["status"] == "awaiting_review"
            assert (repo / "app.py").read_text() == BAD
            assert (await client.get(f"/tasks/{identifier}/events")).status_code == 200
            response = await client.post(f"/tasks/{identifier}/approve", json=decision(record).model_dump())
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "approved"
            report = (await client.get(f"/tasks/{identifier}/candidate-report.json", params=binding)).json()
            assert report["task"]["status"] == "approved"
            assert report["verification"]["approval"]["state"] == "passed"
            history = (await client.get("/tasks", params={"status": "awaiting_review"})).json()
            assert history["tasks"] == []
            history = (await client.get("/tasks", params={"status": "approved"})).json()
            assert [row["id"] for row in history["tasks"]] == [identifier]
            assert (repo / "app.py").read_text() == GOOD
    finally:
        app.state.task_manager.close()
