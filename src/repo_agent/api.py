"""FastAPI application exposing Repo Agent as an asynchronous task service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from . import __version__
from .memory import InMemorySessionMemory, MemoryTurn, RedisSessionMemory, SessionMemory
from .retrieval import RedisChunkCache
from .service import ServiceTask, execute_task
from .task_queue import QueuedTask, RedisTaskQueue, TaskQueue, TaskQueueError
from .task_store import InMemoryTaskStore, RedisTaskStore, TaskStore
from .workspace_lock import (
    InMemoryWorkspaceLock,
    RedisWorkspaceLock,
    WorkspaceLock,
    WorkspaceLockError,
)

TERMINAL_STATUSES = {"completed", "max_steps", "error", "cancelled"}
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskRequest(BaseModel):
    task: str = Field(min_length=1, max_length=20_000)
    workspace: str = "."
    provider: Literal["openrouter", "groq", "huggingface", "mock"] | None = None
    model: str | None = None
    max_steps: int = Field(default=5, ge=1, le=100)
    top_k: int = Field(default=5, ge=1, le=50)
    session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class TaskAccepted(BaseModel):
    id: str
    status: str
    session_id: str


class SessionRequest(BaseModel):
    workspace: str = "."


class TaskManager:
    def __init__(
        self,
        workspace_root: Path,
        *,
        redis_url: str | None,
        workers: int = 2,
        session_ttl: int = 86_400,
        session_max_turns: int = 8,
        task_ttl: int = 604_800,
        task_store: TaskStore | None = None,
        task_queue: TaskQueue | None = None,
        queue_reclaim_after_ms: int = 30_000,
        workspace_lock: WorkspaceLock | None = None,
        workspace_lock_lease_ms: int = 30_000,
        environment_config: dict | None = None,
    ):
        self.workspace_root = workspace_root.expanduser().resolve()
        self.cache = RedisChunkCache(redis_url) if redis_url else None
        self.memory: SessionMemory = (
            RedisSessionMemory(
                redis_url,
                ttl_seconds=session_ttl,
                max_turns=session_max_turns,
            )
            if redis_url
            else InMemorySessionMemory(
                ttl_seconds=session_ttl,
                max_turns=session_max_turns,
            )
        )
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repo-agent")
        self.task_store = task_store or (
            RedisTaskStore(redis_url, ttl_seconds=task_ttl)
            if redis_url
            else InMemoryTaskStore()
        )
        self.task_queue = task_queue or (
            RedisTaskQueue(redis_url, reclaim_after_ms=queue_reclaim_after_ms)
            if redis_url
            else None
        )
        self.workspace_lock = workspace_lock or (
            RedisWorkspaceLock(redis_url, lease_ms=workspace_lock_lease_ms)
            if redis_url
            else InMemoryWorkspaceLock()
        )
        self.environment_config = dict(environment_config or {})
        self._lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._futures: dict[str, Future] = {}
        self._queued_messages: dict[str, str] = {}
        self._stop_dispatcher = threading.Event()
        self._dispatcher = None
        if self.task_queue is not None:
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name="repo-agent-dispatcher",
                daemon=True,
            )
            self._dispatcher.start()

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
        session_id = request.session_id or uuid4().hex
        record = {
            "id": task_id,
            "session_id": session_id,
            "status": "queued",
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "index": None,
            "memory_turns": None,
            "cancel_requested": False,
        }
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[task_id] = cancel_event
        self.task_store.put(record)
        provider = request.provider or os.getenv("REPO_AGENT_PROVIDER", "mock")
        if self.task_queue is not None:
            try:
                self.task_queue.publish({
                    "task_id": task_id,
                    "session_id": session_id,
                    "request": request.model_dump(),
                    "workspace": str(workspace),
                    "provider": provider,
                })
            except TaskQueueError:
                self._update(task_id, status="error", error="Task queue is unavailable", finished_at=_now())
                with self._lock:
                    self._cancel_events.pop(task_id, None)
                raise
        else:
            self._start_execution(task_id, session_id, request, workspace, provider, cancel_event)
        return dict(record)

    def _start_execution(
        self,
        task_id: str,
        session_id: str,
        request: TaskRequest,
        workspace: Path,
        provider: str,
        cancel_event: threading.Event,
        queued_message_id: str | None = None,
    ) -> None:
        future = self.executor.submit(
            self._run, task_id, session_id, request, workspace, provider, cancel_event
        )
        with self._lock:
            self._futures[task_id] = future
            if queued_message_id is not None:
                self._queued_messages[queued_message_id] = task_id
        future.add_done_callback(
            lambda completed, identifier=task_id, message_id=queued_message_id: (
                self._finish_execution(identifier, completed, message_id)
            )
        )

    def _dispatch_loop(self) -> None:
        assert self.task_queue is not None
        while not self._stop_dispatcher.is_set():
            try:
                with self._lock:
                    inflight = list(self._queued_messages)
                self.task_queue.touch(inflight)
                queued = self.task_queue.read(block_ms=1_000)
                if queued is not None:
                    self._dispatch(queued)
            except TaskQueueError as error:
                logger.warning("Redis task queue operation failed; retrying: %s", error)
                self._stop_dispatcher.wait(1.0)

    def _dispatch(self, queued: QueuedTask) -> None:
        assert self.task_queue is not None
        payload = queued.payload
        task_id = str(payload.get("task_id", ""))
        record = self.get(task_id)
        if not task_id or record is None or record["status"] in TERMINAL_STATUSES:
            self.task_queue.acknowledge(queued.message_id)
            return
        try:
            request = TaskRequest.model_validate(payload["request"])
            workspace = self._workspace(str(payload["workspace"]))
            session_id = str(payload["session_id"])
            provider = str(payload["provider"])
        except (KeyError, TypeError, ValueError) as error:
            self._update(task_id, status="error", error=f"Invalid queued task: {error}", finished_at=_now())
            self.task_queue.acknowledge(queued.message_id)
            return

        cancel_event = threading.Event()
        if record.get("cancel_requested"):
            cancel_event.set()
        with self._lock:
            self._cancel_events[task_id] = cancel_event
        self._start_execution(
            task_id,
            session_id,
            request,
            workspace,
            provider,
            cancel_event,
            queued.message_id,
        )

    def _run(
        self,
        task_id: str,
        session_id: str,
        request: TaskRequest,
        workspace: Path,
        provider: str,
        cancel_event: threading.Event,
    ) -> None:
        if cancel_event.is_set():
            self._update(task_id, status="cancelled", finished_at=_now())
            return
        lease = None
        try:
            lease = self.workspace_lock.acquire(
                workspace,
                cancelled=lambda: self._is_cancelled(task_id, cancel_event),
            )
            if lease is None:
                self._update(task_id, status="cancelled", finished_at=_now())
                return
            record = self.get(task_id)
            if record is None or record["status"] in TERMINAL_STATUSES:
                return
            self._update(task_id, status="running", started_at=_now())
            memory = self.memory.load(session_id, workspace)
            self._update(task_id, memory_turns=len(memory))
            output = execute_task(
                ServiceTask(
                    task=request.task,
                    workspace=workspace,
                    provider=provider,
                    model=request.model or os.getenv("REPO_AGENT_MODEL"),
                    max_steps=request.max_steps,
                    top_k=request.top_k,
                    memory=tuple(memory),
                    cancellation_check=lambda: (
                        lease.lost or self._is_cancelled(task_id, cancel_event)
                    ),
                    environment_config=self.environment_config,
                ),
                cache=self.cache,
            )
            if lease.lost:
                self._update(
                    task_id,
                    status="error",
                    error="Workspace lock lease was lost during execution",
                    finished_at=_now(),
                )
                return
            if output.trajectory["status"] == "completed":
                self.memory.append(
                    session_id,
                    workspace,
                    MemoryTurn(request.task, output.trajectory["answer"], _now()),
                )
            self._update(
                task_id,
                status=output.trajectory["status"],
                result=output.trajectory,
                index=output.index,
                error=(
                    output.trajectory["answer"]
                    if output.trajectory["status"] == "error"
                    else None
                ),
                finished_at=_now(),
            )
        except Exception as error:  # noqa: BLE001 - background failures become task state.
            self._update(
                task_id,
                status="error",
                error=f"{type(error).__name__}: {error}",
                finished_at=_now(),
            )
        finally:
            if lease is not None:
                try:
                    lease.release()
                except WorkspaceLockError as error:
                    logger.warning("Workspace lock release failed: %s", error)
                    self._mark_lock_failure(task_id, str(error))
                else:
                    if lease.lost:
                        self._mark_lock_failure(task_id, "Workspace lock lease was lost")

    def _mark_lock_failure(self, task_id: str, message: str) -> None:
        record = self.get(task_id)
        if record is not None and record["status"] not in {"error", "cancelled"}:
            self._update(task_id, status="error", error=message, finished_at=_now())

    def _update(self, task_id: str, **changes) -> None:
        self.task_store.update(task_id, changes)

    def get(self, task_id: str) -> dict | None:
        return self.task_store.get(task_id)

    def cancel(self, task_id: str) -> dict | None:
        record = self.get(task_id)
        if record is None or record["status"] in TERMINAL_STATUSES:
            return record
        with self._lock:
            event = self._cancel_events.get(task_id)
            future = self._futures.get(task_id)
            if event is not None:
                event.set()
        self._update(task_id, cancel_requested=True)
        if record["status"] == "queued" and (future is None or future.cancel()):
            self._update(task_id, status="cancelled", finished_at=_now())
        return self.get(task_id)

    def _is_cancelled(self, task_id: str, event: threading.Event) -> bool:
        if event.is_set():
            return True
        record = self.get(task_id)
        return bool(record and record.get("cancel_requested"))

    def _finish_execution(
        self,
        task_id: str,
        future: Future,
        queued_message_id: str | None,
    ) -> None:
        with self._lock:
            if self._futures.get(task_id) is future:
                self._futures.pop(task_id, None)
                self._cancel_events.pop(task_id, None)
            if queued_message_id is not None:
                self._queued_messages.pop(queued_message_id, None)
        if (
            queued_message_id is not None
            and not future.cancelled()
            and self.task_queue is not None
        ):
            try:
                self.task_queue.acknowledge(queued_message_id)
            except TaskQueueError as error:
                logger.warning("Could not acknowledge completed task; it will be reclaimed: %s", error)

    def create_session(self, workspace: str) -> dict:
        resolved = self._workspace(workspace)
        return {"session_id": uuid4().hex, "workspace": str(resolved)}

    def get_session(self, session_id: str, workspace: str) -> dict:
        resolved = self._workspace(workspace)
        turns = self.memory.load(session_id, resolved)
        return {
            "session_id": session_id,
            "workspace": str(resolved),
            "turns": [
                {"task": turn.task, "answer": turn.answer, "created_at": turn.created_at}
                for turn in turns
            ],
        }

    def close(self) -> None:
        self._stop_dispatcher.set()
        if self._dispatcher is not None:
            self._dispatcher.join(timeout=2)
        if self.task_queue is None:
            with self._lock:
                for event in self._cancel_events.values():
                    event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)


def create_app() -> FastAPI:
    workspace_root = Path(os.getenv("REPO_AGENT_WORKSPACE_ROOT", "/workspace"))
    environment_class = os.getenv("REPO_AGENT_ENVIRONMENT", "local")
    environment_config = {
        "environment_class": environment_class,
        "env_allowlist": [
            name.strip()
            for name in os.getenv("REPO_AGENT_ENV_ALLOWLIST", "").split(",")
            if name.strip()
        ],
    }
    if environment_class == "docker":
        environment_config.update({
            "image": os.getenv("REPO_AGENT_DOCKER_IMAGE", "python:3.13-slim"),
            "network": os.getenv("REPO_AGENT_DOCKER_NETWORK", "none"),
            "memory": os.getenv("REPO_AGENT_DOCKER_MEMORY", "1g"),
            "cpus": float(os.getenv("REPO_AGENT_DOCKER_CPUS", "1")),
            "pids_limit": int(os.getenv("REPO_AGENT_DOCKER_PIDS_LIMIT", "256")),
            "read_only": os.getenv("REPO_AGENT_DOCKER_READ_ONLY", "true").lower()
            not in {"0", "false", "no"},
            "pull": os.getenv("REPO_AGENT_DOCKER_PULL", "never"),
        })
    manager = TaskManager(
        workspace_root,
        redis_url=os.getenv("REDIS_URL"),
        workers=int(os.getenv("REPO_AGENT_WORKERS", "2")),
        session_ttl=int(os.getenv("REPO_AGENT_SESSION_TTL", "86400")),
        session_max_turns=int(os.getenv("REPO_AGENT_SESSION_MAX_TURNS", "8")),
        task_ttl=int(os.getenv("REPO_AGENT_TASK_TTL", "604800")),
        queue_reclaim_after_ms=int(os.getenv("REPO_AGENT_QUEUE_RECLAIM_MS", "30000")),
        workspace_lock_lease_ms=int(os.getenv("REPO_AGENT_WORKSPACE_LOCK_LEASE_MS", "30000")),
        environment_config=environment_config,
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
        except TaskQueueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        response.headers["Location"] = f"/tasks/{record['id']}"
        return record

    @application.post("/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(request: SessionRequest) -> dict:
        try:
            return manager.create_session(request.workspace)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @application.get("/sessions/{session_id}")
    async def get_session(session_id: str, workspace: str = ".") -> dict:
        if not session_id or len(session_id) > 64 or not session_id.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(status_code=422, detail="Invalid session id")
        try:
            return manager.get_session(session_id, workspace)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @application.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict:
        record = manager.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return record

    @application.post("/tasks/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    async def cancel_task(task_id: str) -> dict:
        previous = manager.get(task_id)
        if previous is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if previous["status"] in TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="Task is already finished")
        record = manager.cancel(task_id)
        assert record is not None
        return record

    @application.get("/tasks/{task_id}/events")
    async def task_events(task_id: str, request: Request) -> StreamingResponse:
        if manager.get(task_id) is None:
            raise HTTPException(status_code=404, detail="Task not found")

        async def stream():
            previous = None
            while True:
                if await request.is_disconnected():
                    return
                record = manager.get(task_id)
                if record is None:
                    return
                snapshot = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                if snapshot != previous:
                    yield f"event: task\ndata: {snapshot}\n\n"
                    previous = snapshot
                if record["status"] in TERMINAL_STATUSES:
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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
