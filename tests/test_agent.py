import copy
import json

import pytest

from repo_agent import AgentResult, AgentStatus
from repo_agent.agents.default import DefaultAgent
from repo_agent.environments.local import ExecutionStatus, LocalEnvironment
from repo_agent.models import MessageModel
from repo_agent.result import AgentStep


def action(content, command):
    return {"role": "assistant", "content": content, "extra": {"actions": [{"command": command}]}}


class SequenceModel(MessageModel):
    def __init__(self, actions):
        super().__init__()
        self.actions = iter(actions)
        self.seen = []

    def query(self, messages):
        self.seen.append(messages)
        return next(self.actions)


def test_linear_trajectory_and_task_rendering(tmp_path):
    model = SequenceModel([action("inspect", "ls -la"), action("summary", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")])
    agent = DefaultAgent(model, LocalEnvironment(str(tmp_path)), system_template="SYS {{ task }}",
                         instance_template="USER {{ task }}")
    result = agent.run("Inspect the project")
    assert result.status is AgentStatus.COMPLETED
    assert agent.messages == list(result.messages)
    assert [message["role"] for message in result.messages] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert result.messages[0]["content"] == "SYS Inspect the project"
    assert result.answer == "summary"


def test_messages_reset_on_each_run(tmp_path):
    model = SequenceModel([action("inspect", "ls -la"), action("first", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
                           action("inspect", "ls -la"),
                           action("second", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")])
    agent = DefaultAgent(model, LocalEnvironment(str(tmp_path)))
    first = agent.run("one")
    second = agent.run("two")
    assert agent.messages == list(second.messages)
    assert first.messages != second.messages
    assert "two" in agent.messages[1]["content"]


def test_cancellation_stops_before_the_next_step(tmp_path):
    cancelled = False

    def cancellation_check():
        nonlocal cancelled
        was_cancelled = cancelled
        cancelled = True
        return was_cancelled

    model = SequenceModel([action("inspect", "pwd"), action("should not run", "pwd")])
    result = DefaultAgent(
        model,
        LocalEnvironment(str(tmp_path)),
        max_steps=2,
        cancellation_check=cancellation_check,
    ).run("Inspect the project")

    assert result.status is AgentStatus.CANCELLED
    assert result.step_count == 1
    assert result.messages[-1]["extra"]["exit_status"] == "cancelled"


def test_submission_rejection_continues_and_normal_commands_are_evidence(tmp_path):
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    model = SequenceModel([action("submit", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
                           action("read", "cat app.py"),
                           action("final summary", "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\nverified'")])
    result = DefaultAgent(model, LocalEnvironment(str(tmp_path)), max_steps=3).run("Explain app.py")
    assert result.status is AgentStatus.COMPLETED
    assert result.answer == "verified"
    assert any("Completion rejected:" in m.get("content", "") for m in result.messages)
    assert result.successful_commands == ("cat app.py",)


def test_rejection_requires_next_command_to_gather_evidence(tmp_path):
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    model = SequenceModel([action("submit", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")])
    result = DefaultAgent(model, LocalEnvironment(str(tmp_path)), max_steps=1).run("Explain app.py")
    rejection = result.messages[-1]["content"]
    assert "next command must be a non-submission shell command" in rejection
    assert "Do not repeat the completion marker" in rejection


def test_submission_uses_assistant_content_when_marker_has_no_payload(tmp_path):
    result = DefaultAgent(SequenceModel([action("inspect", "ls -la"), action("readable summary", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]),
                          LocalEnvironment(str(tmp_path))).run("Keep inspecting")
    assert result.answer == "readable summary"
    assert result.steps[1].submission == ""


def test_save_load_and_redact_trajectory(tmp_path):
    agent = DefaultAgent(SequenceModel([action("inspect", "ls -la"), action("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]),
                         LocalEnvironment(str(tmp_path)), component_config={
                             "model": {"api_key": "secret", "nested": {"access_token": "token"}},
                             "run": {"password": "pw"}})
    agent.run("Inspect the project")
    path = tmp_path / "trajectory.json"
    agent.save(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == "0.1.0"
    assert saved["task"] == "Inspect the project"
    assert saved["steps"][0]["execution_status"] == "success"
    assert saved["messages"] == list(agent.messages)
    assert "secret" not in json.dumps(saved)
    assert "token-value" not in json.dumps(saved)
    assert "pw" not in json.dumps(saved)


def test_template_can_reference_environment_and_model(tmp_path):
    model = SequenceModel([action("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")])
    model.model_name = "test/model"
    result = DefaultAgent(model, LocalEnvironment(str(tmp_path)),
                           system_template="cwd={{ cwd }} model={{ model_name }} steps={{ max_steps }}",
                           instance_template="task={{ task }} cwd={{ cwd }} model={{ model_name }}").run("hello")
    assert str(tmp_path) in result.messages[0]["content"]
    assert "test/model" in result.messages[0]["content"]
    assert "hello" in result.messages[1]["content"]


def test_timeout_and_result_evidence(tmp_path):
    model = SequenceModel([action("run", "sleep 1")])
    result = DefaultAgent(model, LocalEnvironment(str(tmp_path), timeout=0.01), max_steps=1).run("Keep inspecting")
    assert result.steps[0].execution_status is ExecutionStatus.TIMED_OUT
    result = AgentResult(AgentStatus.MAX_STEPS, "", (AgentStep(1, "x", "x", execution_status=ExecutionStatus.FAILED),
                                                        AgentStep(2, "y", "y", execution_status=ExecutionStatus.SUCCESS)))
    assert result.successful_commands == ("y",)


def test_model_failure_becomes_serializable_error_result(tmp_path):
    class FailingModel(SequenceModel):
        def query(self, messages):
            raise RuntimeError("provider unavailable")

    agent = DefaultAgent(FailingModel([]), LocalEnvironment(str(tmp_path)))
    result = agent.run("Inspect the project")
    assert result.status is AgentStatus.ERROR
    assert result.answer == "provider unavailable"
    assert result.messages[-1]["role"] == "exit"
    assert agent.serialize()["status"] == "error"


def test_resume_preserves_evidence_and_continues_step_numbers(tmp_path):
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    first = DefaultAgent(
        SequenceModel([action("read", "cat app.py")]),
        LocalEnvironment(str(tmp_path)),
        max_steps=1,
    )
    assert first.run("Explain app.py").status is AgentStatus.MAX_STEPS

    resumed_model = SequenceModel([action("verified", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")])
    resumed = DefaultAgent(resumed_model, LocalEnvironment(str(tmp_path)), max_steps=1)
    result = resumed.resume("Explain app.py", first.serialize())

    assert result.status is AgentStatus.COMPLETED
    assert [step.number for step in result.steps] == [1, 2]
    assert result.successful_commands == ("cat app.py",)
    assert resumed.resumed_from_step == 1
    assert sum(message["role"] == "system" for message in result.messages) == 1
    assert "additional steps" in resumed_model.seen[0][-3]["content"]
    assert "workspace may have changed" in resumed_model.seen[0][-3]["content"]


def test_resume_infers_steps_from_legacy_messages(tmp_path):
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    first = DefaultAgent(
        SequenceModel([action("read", "head app.py")]),
        LocalEnvironment(str(tmp_path)),
        max_steps=1,
    )
    first.run("Explain app.py")
    legacy = copy.deepcopy(first.serialize())
    legacy.pop("steps")
    legacy.pop("task")

    resumed = DefaultAgent(
        SequenceModel([action("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]),
        LocalEnvironment(str(tmp_path)),
        max_steps=1,
    )
    result = resumed.resume("Explain app.py", legacy)
    assert result.status is AgentStatus.COMPLETED
    assert result.successful_commands == ("head app.py",)


def test_completed_trajectory_cannot_be_resumed(tmp_path):
    agent = DefaultAgent(
        SequenceModel([action("inspect", "ls -la"), action("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]),
        LocalEnvironment(str(tmp_path)),
    )
    agent.run("Inspect the project")
    with pytest.raises(ValueError, match="completed trajectory"):
        agent.resume("Inspect the project", agent.serialize())


def test_no_filename_task_cannot_complete_without_a_command(tmp_path):
    result = DefaultAgent(
        SequenceModel([action("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]),
        LocalEnvironment(str(tmp_path)), max_steps=1,
    ).run("Fix the login timeout")
    assert result.status is AgentStatus.MAX_STEPS
    assert "successful non-submission" in result.steps[-1].completion_rejection


def test_failed_verification_rejects_then_rechecks_after_fix(tmp_path):
    model = SequenceModel([
        action("inspect", "ls -la"),
        action("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
        action("fix", "printf ready > result.txt"),
        action("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ])
    agent = DefaultAgent(model, LocalEnvironment(str(tmp_path)), max_steps=4,
                         verification_commands=["test -f result.txt", "grep -q ready result.txt"])
    result = agent.run("Implement the feature")
    assert result.status is AgentStatus.COMPLETED
    assert "Required verification failed" in result.steps[1].completion_rejection
    checks = agent.serialize()["verifications"]
    assert [check["status"] for check in checks] == ["failed", "success", "success"]
    assert [check["submission_step"] for check in checks] == [2, 4, 4]
    assert "Required verification:" in result.messages[-1]["content"]


def test_resume_does_not_reuse_successful_verification(tmp_path):
    first = DefaultAgent(SequenceModel([
        action("inspect", "ls -la"), action("submit", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]), LocalEnvironment(str(tmp_path)), max_steps=2,
        verification_commands=["true", "test -f missing"])
    assert first.run("Implement feature").status is AgentStatus.MAX_STEPS
    resumed = DefaultAgent(SequenceModel([
        action("submit", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]), LocalEnvironment(str(tmp_path)), max_steps=1, verification_commands=["false"])
    result = resumed.resume("Implement feature", first.serialize())
    assert result.status is AgentStatus.MAX_STEPS
    assert resumed.serialize()["verifications"][-1]["command"] == "false"


@pytest.mark.parametrize("commands", ["pytest", [""], [None]])
def test_invalid_verification_configuration_is_rejected(tmp_path, commands):
    with pytest.raises(ValueError, match="verification_commands"):
        DefaultAgent(SequenceModel([]), LocalEnvironment(str(tmp_path)), verification_commands=commands)


@pytest.mark.parametrize("command, status", [("sleep 1", "timed_out"), ("git reset --hard", "rejected")])
def test_verification_respects_execution_limits(tmp_path, command, status):
    agent = DefaultAgent(SequenceModel([
        action("inspect", "ls -la"), action("submit", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]), LocalEnvironment(str(tmp_path), timeout=0.1), max_steps=2, verification_commands=[command])
    assert agent.run("Inspect project").status is AgentStatus.MAX_STEPS
    assert agent.serialize()["verifications"][0]["status"] == status


def test_cancellation_during_verification_stops_remaining_checks(tmp_path):
    class CancellingEnvironment(LocalEnvironment):
        cancelled = False

        def execute(self, command):
            result = super().execute(command)
            if command == "true":
                self.cancelled = True
            return result

    environment = CancellingEnvironment(str(tmp_path))
    agent = DefaultAgent(SequenceModel([
        action("inspect", "ls -la"), action("submit", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]), environment, max_steps=2, verification_commands=["true", "touch should-not-exist"],
        cancellation_check=lambda: environment.cancelled)
    assert agent.run("Inspect project").status is AgentStatus.CANCELLED
    assert len(agent.serialize()["verifications"]) == 1
    assert not (tmp_path / "should-not-exist").exists()


def test_same_read_output_from_different_commands_triggers_recovery_hint(tmp_path):
    (tmp_path / "README.md").write_text("timeout setting\n")
    model = SequenceModel([
        action("read", "grep timeout README.md"),
        action("expand", "grep -A 3 timeout README.md"),
        action("expand again", "grep -A 10 timeout README.md"),
    ])
    result = DefaultAgent(model, LocalEnvironment(str(tmp_path)), max_steps=3).run("Explain timeout")
    assert result.status is AgentStatus.MAX_STEPS
    assert result.messages[-1]["content"].startswith("Inspection is repeating:")
    assert "complete relevant implementation" in result.messages[-1]["content"]


@pytest.mark.parametrize("command", ["printf identical", "grep missing README.md"])
def test_recovery_hint_does_not_flag_non_reads_or_failed_reads(tmp_path, command):
    (tmp_path / "README.md").write_text("timeout setting\n")
    result = DefaultAgent(SequenceModel([action("run", command) for _ in range(3)]),
                          LocalEnvironment(str(tmp_path)), max_steps=3).run("Investigate")
    assert not any(message["content"].startswith("Inspection is repeating:") for message in result.messages)


def test_budget_hints_reach_model_before_actions_and_do_not_auto_submit(tmp_path):
    class RecordingModel(SequenceModel):
        def query(self, messages):
            self.seen.append(copy.deepcopy(messages))
            return next(self.actions)

    (tmp_path / "README.md").write_text("unchanged\n")
    model = RecordingModel([action("inspect", "pwd") for _ in range(5)])
    result = DefaultAgent(model, LocalEnvironment(str(tmp_path)), max_steps=5,
                          verification_commands=["true"]).run("Fix the code; preserve README.md")
    assert result.status is AgentStatus.MAX_STEPS
    assert result.step_count == 5
    assert not any("Step budget:" in m["content"] for m in model.seen[1])
    assert model.seen[2][-1]["content"].startswith("Step budget: 3")
    assert "README.md" in model.seen[2][-1]["content"]
    assert "Current file-evidence blocker" in model.seen[2][-1]["content"]
    assert "outside the model-action budget" in model.seen[2][-1]["content"]
    assert model.seen[4][-1]["content"].startswith("Step budget: 1")
    assert sum(m["content"].startswith("Step budget:") for m in result.messages) == 2


def test_resume_budget_is_additional_not_total_steps(tmp_path):
    first = DefaultAgent(SequenceModel([action("inspect", "pwd")]),
                         LocalEnvironment(str(tmp_path)), max_steps=1)
    first.run("Investigate")
    resumed = DefaultAgent(SequenceModel([action("inspect", "pwd") for _ in range(4)]),
                           LocalEnvironment(str(tmp_path)), max_steps=4)
    result = resumed.resume("Investigate", first.serialize())
    assert result.status is AgentStatus.MAX_STEPS
    assert result.step_count == 5
    hints = [m["content"] for m in result.messages if m["content"].startswith("Step budget:")]
    assert len(hints) == 2
    assert hints[0].startswith("Step budget: 3")
    assert hints[1].startswith("Step budget: 1")
    assert "after the last code change" in hints[0]


def test_budget_hint_does_not_invoke_custom_completion_policy(tmp_path):
    class SubmissionOnlyPolicy:
        def evaluate(self, context):
            raise AssertionError("No submission was attempted")

    result = DefaultAgent(SequenceModel([action("inspect", "pwd") for _ in range(4)]),
                          LocalEnvironment(str(tmp_path)), max_steps=4,
                          completion_policy=SubmissionOnlyPolicy()).run("Investigate")
    assert result.status is AgentStatus.MAX_STEPS


def test_passing_test_before_last_edit_cannot_bypass_required_verification(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    check = "python3 -c 'from app import value; assert value == 1'"
    agent = DefaultAgent(SequenceModel([
        action("inspect", "cat app.py"),
        action("test", check),
        action("change", {"type": "edit", "mode": "replace", "path": "app.py",
                          "old_text": "value = 1", "new_text": "value = 20"}),
        action("submit", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]), LocalEnvironment(str(tmp_path)), max_steps=4, verification_commands=[check])
    result = agent.run("Update app.py")
    assert result.steps[1].execution_status is ExecutionStatus.SUCCESS
    assert result.steps[2].execution_status is ExecutionStatus.SUCCESS
    assert result.status is AgentStatus.MAX_STEPS
    assert "Required verification failed" in result.steps[-1].completion_rejection
    assert agent.serialize()["verifications"][-1]["submission_step"] == 4
    assert agent.serialize()["verifications"][-1]["status"] == "failed"


@pytest.mark.parametrize("checks, state, accepted", [
    ([], "not_configured", True), (["true"], "passed", True), (["false"], "not_accepted", False),
])
def test_handoff_separates_runner_verification_from_model_claims(tmp_path, checks, state, accepted):
    agent = DefaultAgent(SequenceModel([
        action("inspect", "pwd"), action("All tests passed!", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]), LocalEnvironment(str(tmp_path)), max_steps=2, verification_commands=checks)
    result = agent.run("Inspect the project")
    assert result.answer == "All tests passed!"  # preserved model prose, not endorsed
    assert result.handoff["submission_accepted"] is accepted
    assert result.handoff["verification"]["state"] == state
    assert result.handoff["verification"]["submission_step"] == 2
    assert result.handoff["verification"]["configured_commands"] == checks
    assert agent.serialize()["handoff"] == result.handoff
    saved = agent.serialize()
    saved["handoff"]["verification"]["state"] = "invented"
    assert result.handoff["verification"]["state"] == state


def test_handoff_lists_successful_edit_actions_not_a_workspace_diff(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    edit = {"type": "edit", "mode": "replace", "path": "app.py",
            "old_text": "value = 1", "new_text": "value = 2"}
    agent = DefaultAgent(SequenceModel([
        action("inspect", "cat app.py"), action("edit", edit),
        action("bad match", edit), action("shell change", "printf extra > shell.txt"),
        action("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]), LocalEnvironment(str(tmp_path)), max_steps=5)
    result = agent.run("Update app.py")
    assert result.handoff["successful_edit_actions"] == [{"step": 2, "path": "app.py", "mode": "replace"}]
    assert (tmp_path / "shell.txt").exists()
    assert "shell changes are not listed" in result.handoff["limitations"][0]


def test_handoff_latest_submission_does_not_reuse_old_checks(tmp_path):
    first = DefaultAgent(SequenceModel([
        action("inspect", "pwd"), action("submit", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]), LocalEnvironment(str(tmp_path)), max_steps=2, verification_commands=["true", "false"])
    first.run("Inspect project")
    resumed = DefaultAgent(SequenceModel([
        action("submit", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]), LocalEnvironment(str(tmp_path)), max_steps=1, verification_commands=["true"])
    result = resumed.resume("Inspect project", first.serialize())
    assert result.handoff["verification"]["state"] == "passed"
    assert result.handoff["verification"]["submission_step"] == 3
    assert [check["command"] for check in result.handoff["verification"]["checks"]] == ["true"]
    assert len(resumed.serialize()["verifications"]) == 3


def test_handoff_does_not_claim_checks_ran_when_file_evidence_rejected(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    agent = DefaultAgent(SequenceModel([
        action("inspect", "pwd"), action("submit", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]), LocalEnvironment(str(tmp_path)), max_steps=2, verification_commands=["true"])
    result = agent.run("Explain app.py")
    assert result.handoff["verification"]["state"] == "not_run"
    assert result.handoff["verification"]["checks"] == []
    rejection = result.messages[-1]["content"]
    assert "provide the complete task summary again" in rejection
    assert "note about resubmitting" in rejection


@pytest.mark.parametrize("cancelled", [True, False])
def test_cancelled_and_provider_error_results_have_truthful_handoff(tmp_path, cancelled):
    agent = DefaultAgent(SequenceModel([]), LocalEnvironment(str(tmp_path)),
                         verification_commands=["true"], cancellation_check=lambda: cancelled)
    result = agent.run("Inspect project")
    assert result.status is (AgentStatus.CANCELLED if cancelled else AgentStatus.ERROR)
    assert result.handoff["submission_accepted"] is False
    assert result.handoff["verification"]["state"] == "not_run"
    assert result.handoff["verification"]["submission_step"] is None
