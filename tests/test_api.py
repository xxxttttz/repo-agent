import asyncio

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
