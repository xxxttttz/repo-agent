import asyncio
import threading
from types import SimpleNamespace

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from repo_agent.api import create_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def wait_for_task(client, task_id):
    for _ in range(300):
        response = await client.get(f"/tasks/{task_id}")
        if response.json()["status"] not in {"queued", "running"}:
            return response.json()
        await asyncio.sleep(0.01)
    raise AssertionError("task did not finish")


@pytest.mark.anyio
async def test_submit_and_poll_task(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    monkeypatch.setenv("REPO_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = create_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/tasks", json={"task": "Inspect project", "provider": "mock", "max_steps": 2})
        assert response.status_code == 202
        task_id = response.json()["id"]
        session_id = response.json()["session_id"]
        assert response.headers["location"] == f"/tasks/{task_id}"

        payload = await wait_for_task(client, task_id)
        assert payload["status"] == "completed"
        assert payload["index"]["files"] == 1
        assert payload["session_id"] == session_id
        assert payload["memory_turns"] == 0

        second = await client.post("/tasks", json={
            "task": "Continue the discussion",
            "provider": "mock",
            "max_steps": 2,
            "session_id": session_id,
        })
        second_payload = await wait_for_task(client, second.json()["id"])
        assert second_payload["memory_turns"] == 1
        prompt = second_payload["result"]["messages"][1]["content"]
        assert "Inspect project" in prompt

        session = await client.get(f"/sessions/{session_id}")
        assert [turn["task"] for turn in session.json()["turns"]] == [
            "Inspect project",
            "Continue the discussion",
        ]
    app.state.task_manager.close()


@pytest.mark.anyio
async def test_create_empty_session(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/sessions", json={"workspace": "."})
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        loaded = await client.get(f"/sessions/{session_id}")
        assert loaded.json()["turns"] == []
    app.state.task_manager.close()


@pytest.mark.anyio
async def test_api_rejects_workspace_outside_root(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/tasks", json={"task": "Inspect", "workspace": str(tmp_path.parent)})
    assert response.status_code == 400
    app.state.task_manager.close()


@pytest.mark.anyio
async def test_task_events_stream_terminal_snapshot(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    monkeypatch.setenv("REPO_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/tasks", json={"task": "Inspect project", "provider": "mock", "max_steps": 2}
        )
        task_id = accepted.json()["id"]
        await wait_for_task(client, task_id)

        response = await client.get(f"/tasks/{task_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: task" in response.text
    assert '\"status\":\"completed\"' in response.text
    app.state.task_manager.close()


@pytest.mark.anyio
async def test_cancel_running_task_at_step_boundary(tmp_path, monkeypatch):
    release = threading.Event()

    def controlled_execute(spec, *, cache=None):
        release.wait(timeout=2)
        status_value = "cancelled" if spec.cancellation_check() else "completed"
        return SimpleNamespace(
            trajectory={"status": status_value, "answer": "stopped", "messages": [], "steps": []},
            index={"files": 0, "chunks": 0, "cache_hits": 0, "cache_misses": 0},
        )

    monkeypatch.setattr("repo_agent.api.execute_task", controlled_execute)
    monkeypatch.setenv("REPO_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("REDIS_URL", raising=False)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post("/tasks", json={"task": "Long task", "provider": "mock"})
        task_id = accepted.json()["id"]
        for _ in range(100):
            if (await client.get(f"/tasks/{task_id}")).json()["status"] == "running":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("task did not start")

        response = await client.post(f"/tasks/{task_id}/cancel")
        assert response.status_code == 202
        assert response.json()["cancel_requested"] is True
        release.set()
        payload = await wait_for_task(client, task_id)

    assert payload["status"] == "cancelled"
    repeated = app.state.task_manager.cancel(task_id)
    assert repeated["status"] == "cancelled"
    app.state.task_manager.close()


@pytest.mark.anyio
async def test_authenticated_task_history_filters_pages_and_omits_large_sensitive_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("REPO_AGENT_API_TOKEN", "history-access")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REPO_AGENT_REPAIR_PROFILES", raising=False)
    (tmp_path / "project").mkdir()
    (tmp_path / "other").mkdir()
    app = create_app()
    store = app.state.task_manager.task_store
    for identifier, second, state, kind, workspace in [
        ("a", 1, "approved", "ci_repair", tmp_path / "project"),
        ("b", 2, "awaiting_review", "ci_repair", tmp_path / "project"),
        ("c", 3, "queued", "task", tmp_path / "other"),
        ("d", 4, "awaiting_review", "ci_repair", tmp_path.parent / "outside"),
    ]:
        store.put({"id": identifier * 32, "task": f"Fix {identifier}", "status": state, "kind": kind,
                   "created_at": f"2026-10-04T10:00:{second:02}.000000+00:00", "source_workspace": str(workspace),
                   "failure_log": "private diagnostic", "candidate": {"diff": "full patch"},
                   "result": {"messages": ["private trajectory"]}, "repair_profile": {"verification_commands": ["secret"]}})
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/tasks")).status_code == 401
            client.headers["Authorization"] = "Bearer history-access"
            response = await client.get("/tasks", params={"limit": 2})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            first = response.json()
            assert [row["id"] for row in first["tasks"]] == ["c" * 32, "b" * 32]
            assert all(not any(key in row for key in ["failure_log", "result", "candidate", "repair_profile"])
                       for row in first["tasks"])
            assert not first["degraded"]
            second = (await client.get("/tasks", params={"limit": 2, "cursor": first["next_cursor"]})).json()
            assert [row["id"] for row in second["tasks"]] == ["a" * 32]
            assert second["next_cursor"] is None
            pending = (await client.get("/tasks", params={"status": "awaiting_review", "kind": "ci_repair"})).json()
            assert [row["id"] for row in pending["tasks"]] == ["b" * 32]
            project = (await client.get("/tasks", params={"workspace": "project"})).json()
            assert [row["id"] for row in project["tasks"]] == ["b" * 32, "a" * 32]
            general = (await client.get("/tasks", params={"kind": "task"})).json()
            assert [row["id"] for row in general["tasks"]] == ["c" * 32]
    finally:
        app.state.task_manager.close()


@pytest.mark.anyio
@pytest.mark.parametrize("params, code", [
    ({"limit": 0}, 422), ({"limit": 101}, 422), ({"cursor": "malformed"}, 400),
    ({"cursor": "A" * 257}, 422), ({"status": "made_up"}, 400),
    ({"kind": "made_up"}, 422), ({"workspace": ".."}, 400),
])
async def test_task_history_rejects_invalid_queries(tmp_path, monkeypatch, params, code):
    monkeypatch.setenv("REPO_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REPO_AGENT_API_TOKEN", raising=False)
    monkeypatch.delenv("REPO_AGENT_REPAIR_PROFILES", raising=False)
    app = create_app()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/tasks", params=params)).status_code == code
    finally:
        app.state.task_manager.close()
