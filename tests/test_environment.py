import pytest

from repo_agent.environments.local import DangerousCommandPolicy, ExecutionStatus, LocalEnvironment


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
