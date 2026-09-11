import pytest

from repo_agent.environments.docker import DockerEnvironment
from repo_agent.environments.local import (
    DangerousCommandPolicy,
    ExecutionStatus,
    LocalEnvironment,
)


def test_successful_command_uses_workspace_as_cwd(tmp_path):
    result = LocalEnvironment(str(tmp_path)).execute("pwd")
    assert result.status is ExecutionStatus.SUCCESS
    assert result.output.strip() == str(tmp_path)
    assert result.error is None


def test_nonzero_exit_is_failed(tmp_path):
    result = LocalEnvironment(str(tmp_path)).execute("sh -c 'printf failure; exit 7'")
    assert result.status is ExecutionStatus.FAILED
    assert result.returncode == 7
    assert result.output == "failure"


def test_timeout_is_structured(tmp_path):
    result = LocalEnvironment(str(tmp_path), timeout=0.05).execute("sleep 1")
    assert result.status is ExecutionStatus.TIMED_OUT
    assert "timed out" in result.error.lower()


def test_output_is_limited(tmp_path):
    result = LocalEnvironment(str(tmp_path), max_output_size=10).execute("printf 123456789012345")
    assert result.output == "1234567890"
    assert result.truncated


@pytest.mark.parametrize("command", ["rm -rf /", "rm -rf ~", "git reset --hard", "echo safe; rm -rf /",
                                      "sh -c 'rm -rf /'"])
def test_dangerous_commands_are_rejected(tmp_path, command):
    result = LocalEnvironment(str(tmp_path)).execute(command)
    assert result.status is ExecutionStatus.REJECTED
    assert "rejected" in result.error.lower()


def test_policy_allows_workspace_relative_delete():
    assert DangerousCommandPolicy.reason("rm -rf ./build") is None


def test_action_dict_and_submission(tmp_path):
    action = {"role": "assistant", "content": "submit", "extra": {"actions": [
        {"command": "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nanswer'"}]}}
    result = LocalEnvironment(str(tmp_path)).execute(action)
    assert result.status is ExecutionStatus.SUCCESS
    assert result.submission == "answer"


def test_local_environment_can_hide_parent_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_TEST_SECRET", "must-not-leak")

    result = LocalEnvironment(str(tmp_path), inherit_env=False).execute("env")

    assert result.status is ExecutionStatus.SUCCESS
    assert "REPO_AGENT_TEST_SECRET" not in result.output
    assert "PATH=" in result.output


def test_docker_environment_builds_restricted_command(tmp_path, monkeypatch):
    captured = {}
    sentinel = object()

    def fake_popen(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr("repo_agent.environments.docker.subprocess.Popen", fake_popen)
    monkeypatch.setenv("ALLOWED_TOKEN", "secret-value")
    monkeypatch.setenv("BLOCKED_TOKEN", "other-secret")
    environment = DockerEnvironment(
        str(tmp_path), image="repo-agent-sandbox:test", env_allowlist=["ALLOWED_TOKEN"]
    )

    process = environment._start_process("printf ok")

    arguments = captured["arguments"]
    assert process is sentinel
    assert arguments[:3] == ["docker", "run", "--rm"]
    assert arguments[3] == "--name"
    assert arguments[4].startswith("repo-agent-")
    assert "--read-only" in arguments
    assert ["--network", "none"] == arguments[arguments.index("--network"):arguments.index("--network") + 2]
    assert ["--cap-drop", "ALL"] == arguments[arguments.index("--cap-drop"):arguments.index("--cap-drop") + 2]
    assert "ALLOWED_TOKEN" in arguments
    assert "BLOCKED_TOKEN" not in arguments
    assert arguments[-4:] == ["repo-agent-sandbox:test", "/bin/sh", "-lc", "printf ok"]
    assert "secret-value" not in environment.serialize()


def test_missing_docker_binary_is_a_structured_failure(tmp_path):
    environment = DockerEnvironment(str(tmp_path), docker_binary="definitely-missing-docker")

    result = environment.execute("pwd")

    assert result.status is ExecutionStatus.FAILED
    assert "Could not start command" in result.error


def test_docker_timeout_cleanup_removes_named_container(tmp_path, monkeypatch):
    removed = []
    environment = DockerEnvironment(str(tmp_path))
    environment._active_container_name = "repo-agent-test"
    monkeypatch.setattr(LocalEnvironment, "_terminate", lambda process: None)
    monkeypatch.setattr(
        "repo_agent.environments.docker.subprocess.run",
        lambda arguments, **kwargs: removed.append(arguments),
    )

    environment._terminate(object())

    assert removed == [["docker", "rm", "--force", "repo-agent-test"]]
