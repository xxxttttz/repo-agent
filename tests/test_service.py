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
