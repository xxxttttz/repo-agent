import json

import pytest

from repo_agent.agents.default import DefaultAgent
from repo_agent.environments.local import LocalEnvironment
from repo_agent.models import MessageModel
from repo_agent.policies.protected import ProtectedFiles
from repo_agent.result import AgentStatus
from repo_agent.run.local import main


class Commands(MessageModel):
    def __init__(self, commands):
        super().__init__()
        self.commands = iter(commands)

    def query(self, messages):
        return self.format_message("working", [{"command": next(self.commands)}])


SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def test_modified_protected_file_rejects_submission_until_restored(tmp_path):
    (tmp_path / "README.md").write_text("original\n")
    agent = DefaultAgent(Commands([
        "printf 'changed\n' > README.md", SUBMIT,
        "printf 'original\n' > README.md", SUBMIT,
    ]), LocalEnvironment(str(tmp_path)), max_steps=4, protected_paths=["README.md"])
    result = agent.run("Implement feature without changing documentation")
    assert result.status is AgentStatus.COMPLETED
    assert "Protected files changed: README.md" in result.steps[1].completion_rejection
    assert result.steps[-1].completion_rejection is None
    assert agent.serialize()["protected_files"]["README.md"].startswith("sha256:")
    assert "original" not in agent.serialize()["protected_files"]["README.md"]


def test_checks_protected_files_after_verification_commands(tmp_path):
    (tmp_path / "README.md").write_text("original")
    agent = DefaultAgent(Commands(["ls", SUBMIT]), LocalEnvironment(str(tmp_path)), max_steps=2,
                         protected_paths=["README.md"], verification_commands=["echo modified > README.md"])
    result = agent.run("Implement feature")
    assert result.status is AgentStatus.MAX_STEPS
    assert agent.serialize()["verifications"][0]["status"] == "success"
    assert "Protected files changed" in result.steps[-1].completion_rejection
    assert result.handoff["verification"]["state"] == "not_accepted"
    assert result.handoff["verification"]["checks"][0]["status"] == "success"
    assert result.handoff["protected_files"]["checked_on_accepted_submission"] is False


def test_resume_uses_original_snapshot_even_if_workspace_changed(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("original")
    first = DefaultAgent(Commands(["ls"]), LocalEnvironment(str(tmp_path)), max_steps=1,
                         protected_paths=["README.md"])
    first.run("Inspect project")
    original_snapshot = first.serialize()["protected_files"]
    path.write_text("changed outside agent")
    resumed = DefaultAgent(Commands([SUBMIT]), LocalEnvironment(str(tmp_path)), max_steps=1,
                           protected_paths=["README.md"])
    result = resumed.resume("Inspect project", first.serialize())
    assert result.status is AgentStatus.MAX_STEPS
    assert resumed.serialize()["protected_files"] == original_snapshot
    assert path.read_text() == "changed outside agent"


def test_resume_rejects_missing_original_snapshot(tmp_path):
    agent = DefaultAgent(Commands([]), LocalEnvironment(str(tmp_path)), protected_paths=["README.md"])
    with pytest.raises(ValueError, match="original snapshot"):
        agent.resume("Inspect", {"messages": [{"role": "user", "content": "task"}]})


@pytest.mark.parametrize("paths", ["README.md", ["../outside"], ["/tmp/outside"], ["."], [""], [None]])
def test_invalid_protected_paths_are_rejected(tmp_path, paths):
    with pytest.raises(ValueError):
        ProtectedFiles(str(tmp_path), paths)


def test_deleted_new_and_chmod_files_are_detected(tmp_path):
    path = tmp_path / "existing.txt"
    path.write_text("original")
    path.chmod(0o644)
    guard = ProtectedFiles(str(tmp_path), ["existing.txt", "new.txt"])
    baseline = guard.capture()
    path.chmod(0o600)
    assert "existing.txt" in guard.check(baseline)
    path.unlink()
    assert "existing.txt" in guard.check(baseline)
    path.write_text("original")
    path.chmod(0o644)
    assert guard.check(baseline) is None
    (tmp_path / "new.txt").write_text("added")
    assert "new.txt" in guard.check(baseline)


def test_symlink_escape_is_rejected_without_following_target(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "outside").symlink_to(tmp_path, target_is_directory=True)
    guard = ProtectedFiles(str(workspace), ["outside/secret"])
    with pytest.raises(ValueError, match="escapes workspace"):
        guard.capture()


def test_replacing_protected_file_with_symlink_fails(tmp_path):
    path = tmp_path / "README.md"
    path.write_text("original")
    guard = ProtectedFiles(str(tmp_path), ["README.md"])
    baseline = guard.capture()
    path.unlink()
    path.symlink_to("missing-target")
    assert "README.md" in guard.check(baseline)


def test_directories_are_not_silently_treated_as_protected_files(tmp_path):
    (tmp_path / "docs").mkdir()
    with pytest.raises(ValueError, match="not a directory"):
        ProtectedFiles(str(tmp_path), ["docs"]).capture()


def test_cli_protection_survives_resume(tmp_path):
    protected = tmp_path / "README.md"
    protected.write_text("original")
    trajectory = tmp_path / "trajectory.json"
    assert main(["--workspace", str(tmp_path), "--provider", "mock", "--max-steps", "1",
                 "--protect", "README.md", "--output", str(trajectory), "Inspect project"]) == 1
    saved = json.loads(trajectory.read_text())
    assert saved["component_config"]["agent"]["protected_paths"] == ["README.md"]
    protected.write_text("externally modified")
    assert main(["--resume", str(trajectory), "--max-steps", "1"]) == 1
    resumed = json.loads(trajectory.read_text())
    assert resumed["protected_files"] == saved["protected_files"]
    assert "Protected files changed" in resumed["steps"][-1]["completion_rejection"]


def test_saved_configuration_uses_actual_runtime_guards(tmp_path):
    agent = DefaultAgent(Commands(["ls"]), LocalEnvironment(str(tmp_path)), max_steps=1,
                         protected_paths=["README.md"], verification_commands=["true"],
                         component_config={"agent": {"protected_paths": [], "verification_commands": []}})
    agent.run("Inspect")
    saved = agent.serialize()["component_config"]["agent"]
    assert saved["protected_paths"] == ["README.md"]
    assert saved["verification_commands"] == ["true"]
