import asyncio
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from repo_agent.api import ProjectCheckRequest, TaskManager, create_app
from repo_agent.repairs import RepairProfile
from repo_agent.task_queue import QueuedTask, TaskQueueError
from repo_agent.workspace_lock import WorkspaceLockError


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def shell(code):
    return shlex.join([sys.executable, "-B", "-c", code])


def configured(**overrides):
    return replace(RepairProfile(id="demo", workspace="project", reproduce_commands=("true",),
                                 verification_commands=("false",), allowed_paths=("app.py",),
                                 provider="huggingface", model="not-called"), **overrides)


def wait(manager, identifier):
    for _ in range(500):
        record = manager.get(identifier)
        if record["status"] not in {"queued", "running"}:
            return record
        time.sleep(.01)
    raise AssertionError("Project check did not finish")


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "app.py").write_text("value = 1\n")
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "baseline")

    def forbidden(*args, **kwargs):
        pytest.fail("Project checks must not run the agent or load a model")

    monkeypatch.setattr("repo_agent.api.execute_task", forbidden)
    monkeypatch.setattr("repo_agent.service.get_model", forbidden)
    return repo


@pytest.mark.parametrize("command, expected", [("true", "not_reproduced"), ("false", "checks_failed")])
def test_check_uses_only_reproduction_commands_and_never_generates_candidates(tmp_path, project, command, expected):
    base = git(project, "rev-parse", "HEAD")
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured(reproduce_commands=(command,))},
                          worktree_root=tmp_path / "managed")
    try:
        record = wait(manager, manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"])
        assert record["status"] == expected, record
        assert record["kind"] == "project_check" and record["profile_id"] == "demo"
        assert record["preflight_report"]["model_invoked"] is False
        assert record["preflight_report"]["source_repository_observations_unchanged"] is True
        assert record["preflight_report"]["execution_mode"] == "managed_worktree"
        assert record["preflight_report"]["executor"] == "local"
        assert [check["command"] for check in record["baseline"]["checks"]] == [command]
        assert all("output" not in check for check in record["baseline"]["checks"])
        assert record["candidate"] is None and record["review"] is None and record["result"] is None
        assert record["worktree_commit"] is None and record["worktree_merged"] is None
        assert record["index"] is None and record["memory_turns"] is None
        assert manager.get_session(record["session_id"], "project")["turns"] == []
        assert record["worktree_cleaned"] is False and Path(record["worktree_path"]).exists()
        assert git(project, "rev-parse", "HEAD") == base
        assert git(project, "status", "--porcelain") == ""
        assert git(Path(record["worktree_path"]), "rev-parse", "HEAD") == base
        history = manager.list_tasks(kind="project_check", task_status=expected)
        assert [item["id"] for item in history["tasks"]] == [record["id"]]
        assert "preflight_report" not in history["tasks"][0]
    finally:
        manager.close()


@pytest.mark.parametrize("change", ["unstaged", "staged", "untracked", "detached"])
def test_source_blockers_do_not_create_worktree_or_execute_checks(tmp_path, project, monkeypatch, change):
    if change == "detached":
        git(project, "checkout", "--detach", "-q")
    else:
        (project / ("extra.txt" if change == "untracked" else "app.py")).write_text("user changes\n")
        if change == "staged":
            git(project, "add", "app.py")
    before = git(project, "status", "--porcelain")
    monkeypatch.setattr("repo_agent.preflight.run_checks", lambda *args, **kwargs: pytest.fail("No checks"))
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured()},
                          worktree_root=tmp_path / "managed")
    try:
        record = wait(manager, manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"])
        assert record["status"] == "blocked", record
        assert record["preflight_report"]["baseline"]["state"] == "not_run"
        assert record["worktree_path"] is None
        assert git(project, "status", "--porcelain") == before
    finally:
        manager.close()


def test_test_side_effects_are_kept_only_in_worktree_without_commit_or_cleanup(tmp_path, project):
    check = shell("from pathlib import Path; Path('app.py').write_text('value = 2\\n')")
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured(reproduce_commands=(check,))},
                          worktree_root=tmp_path / "managed")
    try:
        record = wait(manager, manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"])
        assert record["status"] == "source_changed", record
        assert record["baseline"]["state"] == "passed"
        assert record["preflight_report"]["source_observations_unchanged"] is False
        assert record["preflight_report"]["source_repository_observations_unchanged"] is True
        assert (project / "app.py").read_text() == "value = 1\n"
        assert (Path(record["worktree_path"]) / "app.py").read_text() == "value = 2\n"
        assert record["candidate"] is None and record["worktree_cleaned"] is False
    finally:
        manager.close()


@pytest.mark.parametrize("check", ["repo_agent_missing_check_command", "sleep 1"])
def test_missing_commands_and_timeouts_do_not_claim_usable_failure(tmp_path, project, check):
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={
        "demo": configured(reproduce_commands=(check,), command_timeout=.05)}, worktree_root=tmp_path / "managed")
    try:
        record = wait(manager, manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"])
        assert record["status"] == "error", record
        assert record["baseline"]["state"] == "error"
        assert record["candidate"] is None
    finally:
        manager.close()


def test_external_source_change_invalidates_a_passing_report(tmp_path, project, monkeypatch):
    from repo_agent.preflight import inspect_project

    def inspect(workspace, commands=(), **kwargs):
        result = inspect_project(workspace, commands, **kwargs)
        if commands:
            (project / "app.py").write_text("# external user change\n")
        return result

    monkeypatch.setattr("repo_agent.api.inspect_project", inspect)
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured()},
                          worktree_root=tmp_path / "managed")
    try:
        record = wait(manager, manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"])
        assert record["status"] == "source_changed"
        assert record["preflight_report"]["source_repository_observations_unchanged"] is False
        assert (project / "app.py").read_text() == "# external user change\n"
    finally:
        manager.close()


@pytest.mark.parametrize("failure", ["lost_before", "lost_during", "lost_on_release", "release_error"])
def test_lock_failures_never_publish_pass(tmp_path, project, monkeypatch, failure):
    from repo_agent.preflight import inspect_project

    class Lease:
        lost = failure == "lost_before"

        def release(self):
            if failure == "release_error":
                raise WorkspaceLockError("release failed")
            if failure == "lost_on_release":
                self.lost = True

    lease = Lease()

    def inspect(workspace, commands=(), **kwargs):
        report = inspect_project(workspace, commands, **kwargs)
        if commands and failure == "lost_during":
            lease.lost = True
        return report

    monkeypatch.setattr("repo_agent.api.inspect_project", inspect)
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured()},
                          worktree_root=tmp_path / "managed",
                          workspace_lock=SimpleNamespace(acquire=lambda *args, **kwargs: lease))
    try:
        record = wait(manager, manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"])
        assert record["status"] == "error", record
        assert record["preflight_report"]["state"] == "error"
        assert record["baseline"] == record["preflight_report"]["baseline"]
        assert record["candidate"] is None
    finally:
        manager.close()


@pytest.mark.parametrize("stop", ["cancel", "deadline"])
def test_cancel_or_deadline_cannot_publish_a_pass(tmp_path, project, monkeypatch, stop):
    entered, released = threading.Event(), threading.Event()
    clock = [0.0]
    monkeypatch.setattr("repo_agent.api.time", SimpleNamespace(monotonic=lambda: clock[0]))

    def checks(*args, **kwargs):
        entered.set()
        assert released.wait(3)
        return {"state": "passed", "checks": []}

    monkeypatch.setattr("repo_agent.preflight.run_checks", checks)
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured()},
                          worktree_root=tmp_path / "managed")
    try:
        identifier = manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"]
        assert entered.wait(3)
        if stop == "cancel":
            manager.cancel(identifier)
        else:
            clock[0] = 1000
        released.set()
        record = wait(manager, identifier)
        assert record["status"] == ("cancelled" if stop == "cancel" else "error")
        assert record["preflight_report"]["state"] == record["status"]
        assert record["candidate"] is None
    finally:
        released.set()
        manager.close()


def test_trusted_environment_configuration_is_forwarded(tmp_path, project, monkeypatch):
    seen = []

    def checks(workspace, commands, config, **kwargs):
        seen.append((workspace, commands, config, kwargs))
        return {"state": "passed", "checks": []}

    monkeypatch.setattr("repo_agent.preflight.run_checks", checks)
    config = {"environment_class": "docker", "image": "trusted-python", "network": "none"}
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured()},
                          worktree_root=tmp_path / "managed", environment_config=config)
    try:
        record = wait(manager, manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"])
        assert record["status"] == "not_reproduced"
        assert seen[0][2] == config
        assert seen[0][3]["timeout"] == 30
        assert record["preflight_report"]["executor"] == "docker"
        assert manager.environment_config == config
    finally:
        manager.close()


def test_unknown_profile_outside_root_and_disabled_worktrees_are_rejected(tmp_path, project):
    manager = TaskManager(tmp_path, redis_url=None, worktree_enabled=False, repair_profiles={
        "demo": configured(), "outside": configured(id="outside", workspace="../")})
    try:
        with pytest.raises(ValueError, match="Unknown"):
            manager.submit_project_check(ProjectCheckRequest(profile="unknown"))
        with pytest.raises(ValueError, match="enabled worktree"):
            manager.submit_project_check(ProjectCheckRequest(profile="demo"))
        with pytest.raises(ValueError, match="inside"):
            manager.submit_project_check(ProjectCheckRequest(profile="outside"))
    finally:
        manager.close()


class ManualQueue:
    def __init__(self):
        self.message = None
        self.acked = []

    def publish(self, payload):
        self.message = QueuedTask("1-0", payload)
        return "1-0"

    def read(self, *, block_ms):
        time.sleep(.01)

    def touch(self, messages):
        pass

    def acknowledge(self, identifier):
        self.acked.append(identifier)


def test_queued_checks_keep_frozen_profile_and_do_not_become_agent_tasks(tmp_path, project):
    queue = ManualQueue()
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured()},
                          worktree_root=tmp_path / "managed", task_queue=queue)
    try:
        identifier = manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"]
        manager.repair_profiles["demo"] = configured(reproduce_commands=("false",))
        manager._dispatch(queue.message)
        record = wait(manager, identifier)
        assert record["status"] == "not_reproduced"
        assert record["baseline"]["checks"][0]["command"] == "true"
        for _ in range(100):
            if queue.acked:
                break
            time.sleep(.01)
        assert queue.acked == ["1-0"]
    finally:
        manager.close()


def test_queued_cancel_and_pending_limit_do_not_execute(tmp_path, project):
    queue = ManualQueue()
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured()},
                          worktree_root=tmp_path / "managed", task_queue=queue, max_pending=1)
    try:
        identifier = manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"]
        with pytest.raises(ValueError, match="Pending-task"):
            manager.submit_project_check(ProjectCheckRequest(profile="demo"))
        assert manager.cancel(identifier)["status"] == "cancelled"
        manager._dispatch(queue.message)
        assert queue.acked == ["1-0"]
        assert manager.get(identifier)["worktree_path"] is None
    finally:
        manager.close()


def test_queue_workspace_override_is_rejected_against_frozen_profile(tmp_path, project):
    queue = ManualQueue()
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured()},
                          worktree_root=tmp_path / "managed", task_queue=queue)
    try:
        identifier = manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"]
        queue.message.payload["workspace"] = str(tmp_path)
        manager._dispatch(queue.message)
        record = wait(manager, identifier)
        assert record["status"] == "error"
        assert "frozen administrator profile" in record["error"]
        assert record["worktree_path"] is None and record["candidate"] is None
    finally:
        manager.close()


def test_waiting_for_repository_lock_can_be_cancelled(tmp_path, project):
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured()},
                          worktree_root=tmp_path / "managed")
    held = manager.workspace_lock.acquire(project, cancelled=lambda: False)
    try:
        identifier = manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"]
        for _ in range(100):
            if manager.get(identifier)["status"] == "running":
                break
            time.sleep(.01)
        manager.cancel(identifier)
        record = wait(manager, identifier)
        assert record["status"] == "cancelled"
        assert record["worktree_path"] is None
    finally:
        held.release()
        manager.close()


def test_unavailable_source_observation_cannot_publish_a_pass(tmp_path, project, monkeypatch):
    from repo_agent.preflight import inspect_project

    observations = []

    def inspect(workspace, commands=(), **kwargs):
        report = inspect_project(workspace, commands, **kwargs)
        if Path(workspace) == project:
            observations.append(report)
            if len(observations) == 2:
                report.update(state="error", source_before=None)
        return report

    monkeypatch.setattr("repo_agent.api.inspect_project", inspect)
    manager = TaskManager(tmp_path, redis_url=None, repair_profiles={"demo": configured()},
                          worktree_root=tmp_path / "managed")
    try:
        record = wait(manager, manager.submit_project_check(ProjectCheckRequest(profile="demo"))["id"])
        assert record["status"] == "error"
        assert record["baseline"]["state"] == "passed"
        assert record["preflight_report"]["source_repository_observations_unchanged"] is None
    finally:
        manager.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_authenticated_console_project_check_history_and_sse(tmp_path, project, monkeypatch):
    config = tmp_path / "profiles.yaml"
    config.write_text(yaml.safe_dump({"profiles": [configured(reproduce_commands=("false",)).serialize()]}))
    monkeypatch.setenv("REPO_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("REPO_AGENT_REPAIR_PROFILES", str(config))
    monkeypatch.setenv("REPO_AGENT_API_TOKEN", "test-access")
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = create_app()
    app.state.task_manager.worktree_manager.root = tmp_path / "managed"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            html = (await client.get("/")).text
            assert 'id="project-check"' in html and 'id="preflight-report"' in html
            assert "api('/project-checks','POST',{profile:" in html
            identifiers = re.findall(r'\bid="([^"]+)"', html)
            assert len(identifiers) == len(set(identifiers))
            assert set(re.findall(r"el\('([^']+)'\)", html)) <= set(identifiers)
            assert (await client.post("/project-checks", json={"profile": "demo"})).status_code == 401
            client.headers["Authorization"] = "Bearer test-access"
            for extra in ["workspace", "commands", "provider", "include_logs", "environment_config"]:
                assert (await client.post("/project-checks", json={"profile": "demo", extra: "override"})).status_code == 422
            assert (await client.post("/project-checks", json={"profile": "unknown"})).status_code == 400
            assert (await client.post("/tasks", json={"task": "bypass"})).status_code == 403
            accepted = await client.post("/project-checks", json={"profile": "demo"})
            assert accepted.status_code == 202 and accepted.headers["cache-control"] == "no-store"
            identifier = accepted.json()["id"]
            assert accepted.headers["location"] == f"/tasks/{identifier}"
            for _ in range(500):
                record = (await client.get(f"/tasks/{identifier}")).json()
                if record["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(.01)
            assert record["status"] == "checks_failed", record
            history = (await client.get("/tasks", params={"kind": "project_check", "status": "checks_failed"})).json()
            assert [item["id"] for item in history["tasks"]] == [identifier]
            assert "preflight_report" not in history["tasks"][0]
            events = await client.get(f"/tasks/{identifier}/events")
            assert events.text.count("event: task") == 1
            assert '"status":"checks_failed"' in events.text
            assert (await client.post(f"/tasks/{identifier}/cancel")).status_code == 409
            assert (await client.get(f"/tasks/{identifier}/candidate")).status_code == 409
            assert (await client.post("/repairs", json={"profile": "demo", "task": "fix"})).status_code == 422
            decision = {"commit": git(project, "rev-parse", "HEAD"), "diff_sha256": "0" * 64}
            assert (await client.post(f"/tasks/{identifier}/approve", json=decision)).status_code == 409
            assert (await client.get(f"/tasks/{identifier}/candidate-report.json", params=decision)).status_code == 409
            monkeypatch.setattr(app.state.task_manager, "submit_project_check",
                                lambda request: (_ for _ in ()).throw(TaskQueueError("unavailable")))
            assert (await client.post("/project-checks", json={"profile": "demo"})).status_code == 503
    finally:
        app.state.task_manager.close()
