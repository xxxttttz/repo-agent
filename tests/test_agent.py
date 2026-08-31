import json
import copy

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
    model = SequenceModel([action("summary", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")])
    agent = DefaultAgent(model, LocalEnvironment(str(tmp_path)), system_template="SYS {{ task }}",
                         instance_template="USER {{ task }}")
    result = agent.run("Inspect the project")
    assert result.status is AgentStatus.COMPLETED
    assert agent.messages == list(result.messages)
    assert [message["role"] for message in result.messages] == ["system", "user", "assistant", "user"]
    assert result.messages[0]["content"] == "SYS Inspect the project"
    assert result.answer == "summary"


def test_messages_reset_on_each_run(tmp_path):
    model = SequenceModel([action("first", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
                           action("second", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")])
    agent = DefaultAgent(model, LocalEnvironment(str(tmp_path)))
    first = agent.run("one")
    second = agent.run("two")
    assert agent.messages == list(second.messages)
    assert first.messages != second.messages
    assert "two" in agent.messages[1]["content"]


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
    result = DefaultAgent(SequenceModel([action("readable summary", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]),
                          LocalEnvironment(str(tmp_path))).run("Keep inspecting")
    assert result.answer == "readable summary"
    assert result.steps[0].submission == ""


def test_save_load_and_redact_trajectory(tmp_path):
    agent = DefaultAgent(SequenceModel([action("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]),
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
        SequenceModel([action("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]),
        LocalEnvironment(str(tmp_path)),
    )
    agent.run("Inspect the project")
    with pytest.raises(ValueError, match="completed trajectory"):
        agent.resume("Inspect the project", agent.serialize())
