import copy
import json
import os
import shlex
import subprocess
import sys

import pytest

from repo_agent.agents.default import DefaultAgent
from repo_agent.environments.docker import DockerEnvironment
from repo_agent.environments.local import ExecutionStatus, LocalEnvironment
from repo_agent.models import (
    GroqModel,
    HuggingFaceModel,
    MessageModel,
    ModelResponseError,
    OpenRouterModel,
)
from repo_agent.models.base import build_api_messages, parse_agent_action
from repo_agent.result import AgentStatus, AgentStep
from repo_agent.tools.edit import EditError, execute_edit


def edit(path="app.py", old="value = 1\n", new="value = 2\n", mode="replace"):
    return {"type": "edit", "mode": mode, "path": path, "old_text": old, "new_text": new}


def test_exact_edit_returns_diff_and_preserves_permissions(tmp_path):
    path = tmp_path / "app.py"
    path.write_bytes(b"# untouched\nvalue = 1\n")
    path.chmod(0o755)
    result = LocalEnvironment(str(tmp_path)).execute(edit())
    assert result.status is ExecutionStatus.SUCCESS
    assert path.read_bytes() == b"# untouched\nvalue = 2\n"
    assert path.stat().st_mode & 0o777 == 0o755
    assert "-value = 1" in result.output and "+value = 2" in result.output
    assert "SHA-256:" in result.output
    assert not list(tmp_path.glob(".repo-agent-edit-*"))


@pytest.mark.parametrize("source, payload", [
    ("value = 1\n", edit(old="missing")),
    ("value = 1\nvalue = 1\n", edit()),
    ("value = 1\n", edit(new="value =\n")),
    ("value = 1\n", edit(old="", new="inserted")),
    ("value = 1\n", edit(new="value = 1\n")),
    ("value = 1\n", edit(mode="create", old="")),
])
def test_failed_edit_leaves_original_file_untouched(tmp_path, source, payload):
    path = tmp_path / "app.py"
    path.write_text(source)
    initial = path.read_bytes()
    result = LocalEnvironment(str(tmp_path)).execute(payload)
    assert result.status is ExecutionStatus.FAILED
    assert path.read_bytes() == initial
    assert not list(tmp_path.glob(".repo-agent-edit-*"))


def test_overlapping_matches_are_ambiguous(tmp_path):
    (tmp_path / "file.txt").write_text("aaa")
    with pytest.raises(EditError, match="ambiguous"):
        execute_edit(edit(path="file.txt", old="aa", new="b"), str(tmp_path))
    assert (tmp_path / "file.txt").read_text() == "aaa"


def test_new_file_is_created_without_shell_interpolation(tmp_path):
    content = "literal $HOME `id` $(touch unexpected) 'quote' \\\n你好\n"
    result = LocalEnvironment(str(tmp_path)).execute(edit(path="new.txt", old="", new=content, mode="create"))
    assert result.status is ExecutionStatus.SUCCESS
    assert (tmp_path / "new.txt").read_text() == content
    assert not (tmp_path / "unexpected").exists()


def test_existing_empty_file_can_be_edited(tmp_path):
    (tmp_path / "app.py").write_bytes(b"")
    assert LocalEnvironment(str(tmp_path)).execute(edit(old="")).status is ExecutionStatus.SUCCESS


def test_crlf_and_unrelated_lines_are_preserved(tmp_path):
    (tmp_path / "app.py").write_bytes(b"# keep\r\nvalue = 1\r\n")
    execute_edit(edit(old="value = 1\r\n", new="value = 2\r\n"), str(tmp_path))
    assert (tmp_path / "app.py").read_bytes() == b"# keep\r\nvalue = 2\r\n"


@pytest.mark.parametrize("path", ["../escape.py", "/tmp/escape.py", ".", ""])
def test_paths_outside_workspace_are_rejected(tmp_path, path):
    result = LocalEnvironment(str(tmp_path)).execute(edit(path=path))
    assert result.status is ExecutionStatus.REJECTED


def test_symlink_files_and_directories_are_never_followed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "target.py"
    target.write_text("value = 1\n")
    (workspace / "link.py").symlink_to(target)
    (workspace / "outside").symlink_to(tmp_path, target_is_directory=True)
    for path in ["link.py", "outside/target.py"]:
        result = LocalEnvironment(str(workspace)).execute(edit(path=path))
        assert result.status is ExecutionStatus.FAILED
    assert target.read_text() == "value = 1\n"


def test_special_files_do_not_block_reading(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    result = LocalEnvironment(str(tmp_path)).execute(edit(path="pipe"))
    assert result.status is ExecutionStatus.FAILED
    assert "regular file" in result.error


def test_atomic_replace_failure_does_not_truncate_original(tmp_path, monkeypatch):
    path = tmp_path / "app.py"
    path.write_text("value = 1\n")

    def fail(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr("repo_agent.tools.edit.os.replace", fail)
    result = LocalEnvironment(str(tmp_path)).execute(edit())
    assert result.status is ExecutionStatus.FAILED
    assert path.read_text() == "value = 1\n"
    assert not list(tmp_path.glob(".repo-agent-edit-*"))


def test_concurrent_create_never_overwrites_the_new_file(tmp_path, monkeypatch):
    path = tmp_path / "new.txt"
    original_link = os.link

    def racing_link(*args, **kwargs):
        path.write_text("concurrent work")
        return original_link(*args, **kwargs)

    monkeypatch.setattr("repo_agent.tools.edit.os.link", racing_link)
    result = LocalEnvironment(str(tmp_path)).execute(edit(path="new.txt", old="", new="agent work", mode="create"))
    assert result.status is ExecutionStatus.FAILED
    assert path.read_text() == "concurrent work"
    assert not list(tmp_path.glob(".repo-agent-edit-*"))


def test_concurrent_replace_preserves_external_changes(tmp_path, monkeypatch):
    from repo_agent.tools import edit as editor

    path = tmp_path / "app.py"
    path.write_text("value = 1\n")
    original_read = editor._read_file
    reads = 0

    def racing_read(*args):
        nonlocal reads
        reads += 1
        if reads == 2:
            path.write_text("value = 99\n")
        return original_read(*args)

    monkeypatch.setattr(editor, "_read_file", racing_read)
    result = LocalEnvironment(str(tmp_path)).execute(edit())
    assert result.status is ExecutionStatus.FAILED
    assert "changed during editing" in result.error
    assert path.read_text() == "value = 99\n"
    assert not list(tmp_path.glob(".repo-agent-edit-*"))


def test_large_files_are_rejected_before_changes(tmp_path):
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    result = LocalEnvironment(str(tmp_path)).execute(edit(path="large.txt", old="x", new="y"))
    assert result.status is ExecutionStatus.FAILED
    assert path.read_bytes().startswith(b"xx")


def test_diff_output_limit_does_not_change_edit_success(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    result = LocalEnvironment(str(tmp_path), max_output_size=8).execute(edit())
    assert result.status is ExecutionStatus.SUCCESS and result.truncated
    assert len(result.output.encode()) == 8
    assert (tmp_path / "app.py").read_text() == "value = 2\n"


@pytest.mark.parametrize("model_type", [OpenRouterModel, GroqModel, HuggingFaceModel])
def test_edit_protocol_round_trip_for_all_providers(model_type):
    payload = {"content": "Update the observed source", "command": edit()}
    model = model_type(model_name="test/model", api_key="test")
    model._send_request = lambda messages: {"choices": [{"message": {"content": json.dumps(payload)}}]}
    response = model.query([{"role": "user", "content": "Fix app.py"}])
    assert response["extra"]["actions"][0]["command"] == edit()
    assert json.loads(build_api_messages([response])[0]["content"]) == payload


@pytest.mark.parametrize("payload", [
    {"type": "unknown"},
    {**edit(), "old_text": None},
    {**edit(), "extra": "ignore this"},
    edit(path="../escape"),
    edit(new="\ud800"),
])
def test_malformed_edit_payload_is_a_model_response_error(payload):
    with pytest.raises(ModelResponseError):
        parse_agent_action(json.dumps({"content": "edit", "command": payload}))


class Actions(MessageModel):
    def __init__(self, commands):
        super().__init__()
        self.commands = iter(commands)

    def query(self, messages):
        return self.format_message("working", [{"command": next(self.commands)}])


def test_agent_edit_verify_save_and_resume(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    first = DefaultAgent(Actions(["cat app.py", edit()]), LocalEnvironment(str(tmp_path)), max_steps=2,
                         verification_commands=["grep -q 'value = 2' app.py"])
    assert first.run("Fix app.py").status is AgentStatus.MAX_STEPS
    saved = json.loads(json.dumps(first.serialize()))
    assert AgentStep.deserialize(saved["steps"][1]).command == edit()
    resumed = DefaultAgent(Actions(["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]),
                           LocalEnvironment(str(tmp_path)), max_steps=1,
                           verification_commands=["grep -q 'value = 2' app.py"])
    result = resumed.resume("Fix app.py", saved)
    assert result.status is AgentStatus.COMPLETED
    assert result.successful_commands == ("cat app.py",)
    assert resumed.serialize()["verifications"][0]["status"] == "success"


def test_structured_edit_cannot_write_protected_file(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    agent = DefaultAgent(Actions([edit(path="./app.py")]), LocalEnvironment(str(tmp_path)), max_steps=1,
                         protected_paths=["app.py"])
    result = agent.run("Inspect")
    assert result.steps[0].execution_status is ExecutionStatus.REJECTED
    assert (tmp_path / "app.py").read_text() == "value = 1\n"


def test_legacy_step_inference_keeps_edit_actions(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    first = DefaultAgent(Actions(["cat app.py", edit()]), LocalEnvironment(str(tmp_path)), max_steps=2)
    first.run("Fix app.py")
    legacy = copy.deepcopy(first.serialize())
    legacy.pop("steps")
    resumed = DefaultAgent(Actions(["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]),
                           LocalEnvironment(str(tmp_path)), max_steps=1)
    result = resumed.resume("Fix app.py", legacy)
    assert result.status is AgentStatus.COMPLETED
    assert [step.number for step in result.steps] == [1, 2, 3]
    assert result.steps[1].command == edit()


def test_docker_edit_uses_container_command_path_not_host_editor(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("value = 1\n")
    commands = []

    def simulated_container(self, command):
        arguments = shlex.split(command)
        commands.append(arguments)
        assert arguments[:2] == ["python3", "-c"]
        assert json.loads(arguments[-1]) == edit()
        return subprocess.Popen([sys.executable, *arguments[1:]], cwd=tmp_path,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)

    monkeypatch.setattr(DockerEnvironment, "_start_process", simulated_container)
    result = DockerEnvironment(str(tmp_path)).execute(edit())
    assert result.status is ExecutionStatus.SUCCESS
    assert commands and (tmp_path / "app.py").read_text() == "value = 2\n"
