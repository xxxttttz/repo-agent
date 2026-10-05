from repo_agent.memory import MemoryTurn
from repo_agent.service import ServiceTask, execute_task


class MemoryCache:
    def __init__(self):
        self.values = {}

    def get(self, key, path):
        chunks = self.values.get(key)
        if chunks is None:
            return None
        return [type(chunk)(path, chunk.name, chunk.start_line, chunk.end_line, chunk.text) for chunk in chunks]

    def set(self, key, chunks):
        self.values[key] = list(chunks)


def test_service_task_runs_with_retrieval_and_reports_cache_hits(tmp_path):
    (tmp_path / "app.py").write_text("def calculate_total():\n    return 42\n", encoding="utf-8")
    cache = MemoryCache()
    spec = ServiceTask("Explain calculate_total", tmp_path, provider="mock", max_steps=2)

    first = execute_task(spec, cache=cache)
    second = execute_task(spec, cache=cache)

    assert first.trajectory["status"] == "completed"
    assert "calculate_total" in first.trajectory["messages"][1]["content"]
    assert first.index["cache_misses"] == 1
    assert second.index["cache_hits"] == 1


def test_service_task_injects_prior_session_memory(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    spec = ServiceTask(
        "What did we decide?",
        tmp_path,
        provider="mock",
        max_steps=2,
        memory=(MemoryTurn("Choose a cache", "Use Redis.", "2026-01-01T00:00:00Z"),),
    )

    result = execute_task(spec)

    prompt = result.trajectory["messages"][1]["content"]
    assert "Prior completed tasks in this session" in prompt
    assert "Choose a cache" in prompt
    assert "Use Redis." in prompt


def test_service_local_environment_does_not_inherit_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_TEST_SECRET", "must-not-leak")
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    result = execute_task(ServiceTask("Inspect project", tmp_path, provider="mock", max_steps=2))

    environment_config = result.trajectory["component_config"]["environment"]
    assert environment_config["inherit_env"] is False


def test_service_honors_required_verification(tmp_path):
    result = execute_task(ServiceTask(
        "Inspect project", tmp_path, provider="mock", max_steps=2,
        verification_commands=["test -f expected.txt"],
    ))
    assert result.trajectory["status"] == "max_steps"
    assert result.trajectory["verifications"][0]["status"] == "failed"
    assert result.trajectory["handoff"]["verification"]["state"] == "not_accepted"
    assert result.trajectory["handoff"]["submission_accepted"] is False


def test_service_records_caller_protected_files(tmp_path):
    (tmp_path / "README.md").write_text("original")
    result = execute_task(ServiceTask("Inspect project", tmp_path, provider="mock", max_steps=2,
                                     protected_paths=["README.md"]))
    assert result.trajectory["status"] == "completed"
    assert result.trajectory["protected_files"]["README.md"].startswith("sha256:")
    assert result.trajectory["handoff"]["protected_files"] == {
        "paths": ["README.md"], "checked_on_accepted_submission": True,
    }


def test_service_passes_runtime_scope_check_without_serializing_it(tmp_path):
    result = execute_task(ServiceTask(
        "Inspect project", tmp_path, provider="mock", max_steps=2,
        submission_scope_check=lambda: {"passed": False, "changed_paths": ["backup.txt"], "error": "Outside scope"},
        submission_scope_description="Allowed paths: app.py",
    ))
    assert result.trajectory["status"] == "max_steps"
    assert result.trajectory["submission_scope_checks"][0]["passed"] is False
    assert "submission_scope_check" not in result.trajectory["component_config"]["agent"]
    assert any("Allowed paths: app.py" in m["content"] for m in result.trajectory["messages"])
