"""FastAPI application exposing Repo Agent as an asynchronous task service."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field

from . import __version__
from .retrieval import RedisChunkCache
from .service import ServiceTask, execute_task


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    workspace: str = "."
    provider: Literal["openrouter", "groq", "huggingface", "mock"] | None = None
    model: str | None = None
    max_steps: int = Field(default=5, ge=1, le=100)
    top_k: int = Field(default=5, ge=1, le=50)


class TaskAccepted(BaseModel):
    id: str
    status: str


class TaskManager:
    def __init__(self, workspace_root: Path, *, redis_url: str | None, workers: int = 2):
        self.workspace_root = workspace_root.expanduser().resolve()
        self.cache = RedisChunkCache(redis_url) if redis_url else None
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repo-agent")
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _workspace(self, requested: str) -> Path:
        candidate = Path(requested)
        resolved = (self.workspace_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if resolved != self.workspace_root and self.workspace_root not in resolved.parents:
            raise ValueError("workspace must be inside REPO_AGENT_WORKSPACE_ROOT")
        if not resolved.is_dir():
            raise ValueError(f"Workspace is not a directory: {requested}")
        return resolved

    def submit(self, request: TaskRequest) -> dict:
        workspace = self._workspace(request.workspace)
        task_id = uuid4().hex
        record = {
            "id": task_id,
            "status": "queued",
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "index": None,
        }
        with self._lock:
            self._tasks[task_id] = record
        provider = request.provider or os.getenv("REPO_AGENT_PROVIDER", "mock")
        self.executor.submit(self._run, task_id, request, workspace, provider)
        return dict(record)

    def _run(self, task_id: str, request: TaskRequest, workspace: Path, provider: str) -> None:
        self._update(task_id, status="running", started_at=_now())
        try:
            output = execute_task(
                ServiceTask(
                    task=request.task,
                    workspace=workspace,
                    provider=provider,
                    model=request.model or os.getenv("REPO_AGENT_MODEL"),
                    max_steps=request.max_steps,
                    top_k=request.top_k,
                ),
                cache=self.cache,
            )
            self._update(
                task_id,
                status=output.trajectory["status"],
                result=output.trajectory,
                index=output.index,
                finished_at=_now(),
            )
        except Exception as error:  # noqa: BLE001 - background failures become task state.
            self._update(
                task_id,
                status="error",
                error=f"{type(error).__name__}: {error}",
                finished_at=_now(),
            )

    def _update(self, task_id: str, **changes) -> None:
        with self._lock:
            self._tasks[task_id].update(changes)

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            record = self._tasks.get(task_id)
            return dict(record) if record else None

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


def create_app() -> FastAPI:
    workspace_root = Path(os.getenv("REPO_AGENT_WORKSPACE_ROOT", "/workspace"))
    manager = TaskManager(
        workspace_root,
        redis_url=os.getenv("REDIS_URL"),
        workers=int(os.getenv("REPO_AGENT_WORKERS", "2")),
    )
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        manager.close()

    application = FastAPI(title="Repo Agent API", version=__version__, lifespan=lifespan)
    application.state.task_manager = manager

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/tasks", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED)
    async def create_task(request: TaskRequest, response: Response) -> dict:
        try:
            record = manager.submit(request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        response.headers["Location"] = f"/tasks/{record['id']}"
        return record

    @application.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict:
        record = manager.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return record

    return application


app = create_app()


def run() -> None:
    """Start the API using the packaged console script."""
    import uvicorn

    uvicorn.run(
        "repo_agent.api:app",
        host=os.getenv("REPO_AGENT_HOST", "0.0.0.0"),
        port=int(os.getenv("REPO_AGENT_PORT", "8000")),
    )
