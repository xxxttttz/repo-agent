import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from repo_agent.api import create_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
        assert response.headers["location"] == f"/tasks/{task_id}"

        for _ in range(300):
            result = await client.get(f"/tasks/{task_id}")
            if result.json()["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.01)

        payload = result.json()
        assert payload["status"] == "completed"
        assert payload["index"]["files"] == 1
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
