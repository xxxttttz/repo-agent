import json

from repo_agent.run.local import build_parser, main, print_result
from repo_agent.environments.local import ExecutionStatus
from repo_agent.result import AgentResult, AgentStatus, AgentStep


def test_print_result_shows_execution_diagnostics(capsys):
    result = AgentResult(AgentStatus.MAX_STEPS, "", (
        AgentStep(1, "Failed", "bad-command", execution_status=ExecutionStatus.FAILED,
                  error="Command exited with return code 1."),
        AgentStep(2, "Trying", "sleep 1", execution_status=ExecutionStatus.TIMED_OUT,
                  error="Command timed out after 0.01 seconds.", output_truncated=True),
        AgentStep(3, "Rejected", "rm -rf /", execution_status=ExecutionStatus.REJECTED,
                  error="Command rejected by safety policy.")))
    print_result(result)
    output = capsys.readouterr().out
    assert "Execution status: failed" in output
    assert "Execution status: timed_out" in output
    assert "Output truncated" in output
    assert "Execution status: rejected" in output


def test_print_result_shows_agent_error(capsys):
    print_result(AgentResult(AgentStatus.ERROR, "provider unavailable", ()))
    assert "Agent stopped with error: provider unavailable" in capsys.readouterr().out


def test_parser_allows_resume_without_positional_task():
    args = build_parser().parse_args(["--resume", "trajectory.json"])
    assert args.task is None
    assert str(args.resume) == "trajectory.json"


def test_cli_resumes_saved_task_workspace_and_mock_provider(tmp_path, capsys):
    trajectory = tmp_path / "trajectory.json"
    first_status = main([
        "--provider", "mock",
        "--workspace", str(tmp_path),
        "--max-steps", "1",
        "--output", str(trajectory),
        "Inspect the directory",
    ])
    assert first_status == 1
    first = json.loads(trajectory.read_text(encoding="utf-8"))
    assert first["status"] == "max_steps"
    assert first["task"] == "Inspect the directory"

    resumed_status = main(["--resume", str(trajectory), "--max-steps", "1"])
    assert resumed_status == 0
    resumed = json.loads(trajectory.read_text(encoding="utf-8"))
    assert resumed["status"] == "completed"
    assert [step["number"] for step in resumed["steps"]] == [1, 2]
    assert resumed["component_config"]["environment"]["cwd"] == str(tmp_path)
    assert "Submitting the result." in capsys.readouterr().out
