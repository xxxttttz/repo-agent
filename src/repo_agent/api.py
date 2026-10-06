"""FastAPI application exposing Repo Agent as an asynchronous task service."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from starlette.datastructures import Headers
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import __version__
from .artifacts import candidate_export
from .memory import InMemorySessionMemory, MemoryTurn, RedisSessionMemory, SessionMemory
from .policies.protected import ProtectedFiles
from .preflight import inspect_project
from .repairs import RepairProfile, load_profiles, run_checks
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
from .worktree import (
    TaskWorktree,
    WorktreeError,
    WorktreeManager,
)

TERMINAL_STATUSES = {
    "completed",
    "max_steps",
    "error",
    "cancelled",
    "merge_conflict",
    "awaiting_review",
    "approved",
    "rejected",
    "not_reproduced",
    "checks_failed",
    "source_changed",
    "blocked",
    "not_configured",
}
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
    delivery_mode: Literal["review", "auto_merge"] = "review"
    verification_commands: list[Annotated[str, Field(min_length=1, max_length=4000)]] | None = Field(
        default=None, max_length=20,
    )
    protected_paths: list[Annotated[str, Field(min_length=1, max_length=1024)]] | None = Field(
        default=None, max_length=100,
    )
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


class RepairRequest(BaseModel):
    model_config = {"extra": "forbid"}
    profile: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=4000)
    failure_log: str = Field(min_length=1, max_length=40_000)

    @field_validator("task", "failure_log")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Repair task and failure log must not be blank")
        return value


class ReviewRequest(BaseModel):
    model_config = {"extra": "forbid"}
    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=2000)


class ProjectCheckRequest(BaseModel):
    model_config = {"extra": "forbid"}
    profile: str = Field(min_length=1, max_length=64)


class TokenAuthMiddleware:
    """Pure ASGI authentication: do not buffer SSE or consume request bodies."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] not in {"/", "/health"}:
            provided = Headers(scope=scope).get("authorization", "").encode()
            if not hmac.compare_digest(provided, self.token):
                response = JSONResponse({"detail": "Bearer token required"}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


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
        worktree_enabled: bool = True,
        worktree_root: Path | None = None,
        worktree_manager: WorktreeManager | None = None,
        repair_profiles: dict[str, RepairProfile] | None = None,
        max_pending: int = 100,
    ):
        self.workspace_root = workspace_root.expanduser().resolve()
        self.repair_profiles = dict(repair_profiles or {})
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.max_pending = max_pending
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

        self.worktree_enabled = worktree_enabled

        self.worktree_manager = worktree_manager or WorktreeManager(
            worktree_root
            or Path(
                os.getenv(
                    "REPO_AGENT_WORKTREE_ROOT",
                    "/tmp/repo-agent-worktrees",
                )
            )
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

    def submit_repair(self, request: RepairRequest) -> dict:
        profile = self.repair_profiles.get(request.profile)
        if profile is None:
            raise ValueError("Unknown repair profile; an administrator must configure it first")
        workspace = self._workspace(profile.workspace)
        if not self.worktree_enabled or self.worktree_manager.repo_root(workspace) != workspace:
            raise ValueError("CI repair requires an enabled worktree and a Git repository root")
        return self.submit(TaskRequest(task=request.task, workspace=profile.workspace,
                                       provider=profile.provider, model=profile.model, max_steps=profile.max_steps,
                                       verification_commands=list(profile.verification_commands),
                                       protected_paths=list(profile.protected_paths), delivery_mode="review"),
                           repair_profile=profile, failure_log=request.failure_log)

    def submit_project_check(self, request: ProjectCheckRequest) -> dict:
        profile = self.repair_profiles.get(request.profile)
        if profile is None:
            raise ValueError("Unknown repair profile; an administrator must configure it first")
        workspace = self._workspace(profile.workspace)
        if not self.worktree_enabled or self.worktree_manager.repo_root(workspace) != workspace:
            raise ValueError("Project checks require an enabled worktree and a Git repository root")
        return self.submit(TaskRequest(task=f"项目接入检查 · {profile.title}", workspace=profile.workspace,
                                       provider="mock", max_steps=1, delivery_mode="review"),
                           project_check_profile=profile)

    def submit(self, request: TaskRequest, *, repair_profile: RepairProfile | None = None,
               failure_log: str = "", project_check_profile: RepairProfile | None = None) -> dict:
        if repair_profile is not None and project_check_profile is not None:
            raise ValueError("A task cannot be both a repair and a project check")
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
        "task": request.task,
        "request": request.model_dump(),
        "delivery_mode": request.delivery_mode,
        "kind": "project_check" if project_check_profile else "ci_repair" if repair_profile else "task",
        "repair_profile": repair_profile.serialize() if repair_profile else None,
        "project_check_profile": project_check_profile.serialize() if project_check_profile else None,
        "profile_id": (project_check_profile or repair_profile).id if project_check_profile or repair_profile else None,
        "preflight_report": None,
        "failure_log": failure_log,
        "baseline": None,
        "post_verification": None,
        "scope_review": None,
        "candidate": None,
        "review": None,

        # Original project path.
        "source_workspace": str(workspace),

        # Actual path used by Agent.
        "execution_workspace": None,

        # Worktree metadata. These stay None for non-Git workspaces.
        "worktree_path": None,
        "worktree_branch": None,
        "worktree_source_branch": None,
        "worktree_base_commit": None,
        "worktree_commit": None,
        "worktree_merged": None,
        "worktree_merge_commit": None,
        "worktree_merge_error": None,
        "worktree_cleaned": None,
        "worktree_cleanup_error": None,
        }
        cancel_event = threading.Event()
        with self._lock:
            for identifier in list(self._cancel_events):
                prior = self.get(identifier)
                if prior is None or prior["status"] in TERMINAL_STATUSES:
                    self._cancel_events.pop(identifier, None)
            if len(self._cancel_events) >= self.max_pending:
                raise ValueError("Pending-task limit reached; retry after another task finishes")
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
        if not task_id or record is None or record["status"] not in {"queued", "running"}:
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

        record = self.get(task_id)
        if record and record.get("kind") == "project_check":
            self._run_project_check(task_id, workspace, cancel_event)
            return

        source_workspace = workspace
        execution_workspace = source_workspace
        execution_lease = None
        task_worktree: TaskWorktree | None = None
        final_commit: str | None = None
        pending_update: dict | None = None

        try:
            repo_root = (
                self.worktree_manager.repo_root(source_workspace)
                if self.worktree_enabled
                else None
            )
            if repo_root is not None:
                create_lease = self.workspace_lock.acquire(
                    repo_root,
                    cancelled=lambda: self._is_cancelled(task_id, cancel_event),
                )
                if create_lease is None:
                    self._update(task_id, status="cancelled", finished_at=_now())
                    return
                try:
                    task_worktree = self.worktree_manager.create(source_workspace, task_id)
                    if create_lease.lost:
                        raise WorktreeError(
                            "Workspace lock lease was lost while creating the task worktree"
                        )
                finally:
                    try:
                        create_lease.release()
                    except WorkspaceLockError as error:
                        raise WorktreeError(
                            "Could not release workspace lock after worktree creation: "
                            f"{error}"
                        ) from error

                execution_workspace = task_worktree.workspace
                self._update(
                    task_id,
                    execution_workspace=str(execution_workspace),
                    worktree_path=str(task_worktree.path),
                    worktree_branch=task_worktree.branch,
                    worktree_source_branch=task_worktree.source_branch,
                    worktree_base_commit=task_worktree.base_commit,
                    worktree_cleaned=False,
                )
            else:
                execution_lease = self.workspace_lock.acquire(
                    source_workspace,
                    cancelled=lambda: self._is_cancelled(task_id, cancel_event),
                )
                if execution_lease is None:
                    self._update(task_id, status="cancelled", finished_at=_now())
                    return
                self._update(task_id, execution_workspace=str(execution_workspace))

            record = self.get(task_id)
            if record is None or record["status"] in TERMINAL_STATUSES:
                return

            self._update(task_id, status="running", started_at=_now())
            profile_data = record.get("repair_profile")
            profile = RepairProfile(**profile_data) if profile_data else None
            deadline = time.monotonic() + profile.deadline_seconds if profile else None
            protected = ProtectedFiles(execution_workspace, profile.protected_paths) if profile else None
            protected_baseline = protected.capture() if protected else {}
            if profile:
                self._update(task_id, repair_protected_baseline=protected_baseline)
            environment_config = dict(self.environment_config)
            if profile:
                environment_config["timeout"] = profile.command_timeout
            memory = self.memory.load(session_id, source_workspace)
            self._update(task_id, memory_turns=len(memory))

            def cancellation_check() -> bool:
                return self._is_cancelled(task_id, cancel_event) or (
                    execution_lease is not None and execution_lease.lost
                ) or (deadline is not None and time.monotonic() >= deadline)

            failure_log = record.get("failure_log", "")
            submission_scope_check = None
            submission_scope_description = ""
            if profile:
                def submission_scope_check() -> dict:
                    changed_paths = self.worktree_manager.changed_paths(task_worktree)
                    scope_error = profile.check_scope(changed_paths)
                    return {"passed": scope_error is None, "changed_paths": changed_paths, "error": scope_error}

                submission_scope_description = (
                    f"Allowed final paths: {list(profile.allowed_paths)}; "
                    f"maximum changed files: {profile.max_changed_files}. "
                    "The service includes tracked, staged, committed and non-ignored untracked "
                    "changes relative to the original base."
                )
                baseline = run_checks(execution_workspace, profile.reproduce_commands, environment_config,
                                      timeout=profile.command_timeout, cancelled=cancellation_check)
                self._update(task_id, baseline=baseline)
                if baseline["state"] != "failed":
                    stopped = "not_reproduced" if baseline["state"] == "passed" else "error"
                    if self._is_cancelled(task_id, cancel_event):
                        stopped = "cancelled"
                    self._update(task_id, status=stopped, finished_at=_now(),
                                 error=None if stopped == "not_reproduced" else "Baseline check could not reproduce a usable failure")
                    return
                failure_log = (
                    f"Caller policy: allowed final paths {list(profile.allowed_paths)}; "
                    f"maximum changed files {profile.max_changed_files}. Preserve protected files "
                    "and avoid optional refactoring. Repair the reproducible failure and add regression tests.\n"
                    f"Submitted failure log:\n{failure_log}\n"
                    f"Runner baseline evidence:\n{json.dumps(baseline, ensure_ascii=False)}"
                )

            output = execute_task(
                ServiceTask(
                    task=request.task,
                    workspace=execution_workspace,
                    provider=provider,
                    model=request.model or os.getenv("REPO_AGENT_MODEL"),
                    max_steps=request.max_steps,
                    top_k=request.top_k,
                    memory=tuple(memory),
                    cancellation_check=cancellation_check,
                    environment_config=environment_config,
                    verification_commands=request.verification_commands,
                    protected_paths=request.protected_paths,
                    failure_log=failure_log,
                    submission_scope_check=submission_scope_check,
                    submission_scope_description=submission_scope_description,
                ),
                cache=self.cache,
            )

            if execution_lease is not None and execution_lease.lost:
                self._update(
                    task_id,
                    status="error",
                    error="Workspace lock lease was lost during execution",
                    finished_at=_now(),
                )
                return

            task_status = output.trajectory["status"]
            if self._is_cancelled(task_id, cancel_event):
                task_status = "cancelled"
            elif deadline is not None and time.monotonic() >= deadline:
                task_status = "error"
                self._update(task_id, error="Repair time budget exceeded (checked at action boundaries)")
            if profile and task_status == "completed":
                verification = run_checks(execution_workspace, profile.verification_commands, environment_config,
                                          timeout=profile.command_timeout, cancelled=cancellation_check)
                self._update(task_id, post_verification=verification)
                if verification["state"] == "cancelled" and self._is_cancelled(task_id, cancel_event):
                    task_status = "cancelled"
                elif verification["state"] != "passed":
                    raise WorktreeError("Final caller-required verification did not pass")
                if task_status == "completed":
                    protection_error = protected.check(protected_baseline)
                    if protection_error:
                        raise WorktreeError(protection_error)
                    changed = self.worktree_manager.changed_paths(task_worktree)
                    scope_error = profile.check_scope(changed)
                    self._update(task_id, scope_review={"passed": scope_error is None, "changed_paths": changed,
                                                       "error": scope_error})
                    if scope_error:
                        raise WorktreeError(scope_error)
            if task_worktree is not None and task_status == "completed":
                first_line = next(iter(request.task.strip().splitlines()), "")
                summary = first_line[:72] if first_line else task_id
                final_commit = self.worktree_manager.commit_all(
                    task_worktree,
                    f"repo-agent: {summary}",
                )
                if final_commit is None:
                    head = self.worktree_manager.head_commit(task_worktree)
                    if head != task_worktree.base_commit:
                        final_commit = head
                self._update(
                    task_id,
                    worktree_commit=final_commit,
                    worktree_merged=False if final_commit is not None else None,
                )
                if request.delivery_mode == "review":
                    if final_commit is None and profile:
                        raise WorktreeError("Repair completed without a candidate patch")
                    if final_commit is not None:
                        candidate = self.worktree_manager.candidate(task_worktree, final_commit)
                        self._update(task_id, candidate=candidate)
                        task_status = "awaiting_review"

            if task_status == "completed" and (task_worktree is None or request.delivery_mode == "auto_merge"):
                self.memory.append(
                    session_id,
                    source_workspace,
                    MemoryTurn(request.task, output.trajectory["answer"], _now()),
                )

            result_update = {
                "status": task_status,
                "result": output.trajectory,
                "index": output.index,
                "error": (
                    self.get(task_id).get("error") or output.trajectory["answer"] if task_status == "error" else None
                ),
                "finished_at": _now(),
            }
            if task_worktree is None:
                self._update(task_id, **result_update)
            else:
                # A terminal task snapshot should include the final cleanup
                # state, so publish it only after the finally block below.
                pending_update = result_update
        except Exception as error:  # noqa: BLE001 - background failures become task state.
            error_update = {
                "status": "error",
                "error": f"{type(error).__name__}: {error}",
                "finished_at": _now(),
            }
            if "output" in locals():
                error_update.update(result=output.trajectory, index=output.index)
            if task_worktree is None:
                self._update(task_id, **error_update)
            else:
                pending_update = error_update
        finally:
            if execution_lease is not None:
                try:
                    execution_lease.release()
                except WorkspaceLockError as error:
                    logger.warning("Workspace lock release failed: %s", error)
                    self._mark_lock_failure(task_id, str(error))
                else:
                    if execution_lease.lost:
                        self._mark_lock_failure(task_id, "Workspace lock lease was lost")

            if task_worktree is not None:
                cleanup_lease = None
                try:
                    cleanup_lease = self.workspace_lock.acquire(
                        task_worktree.repo_root,
                        cancelled=lambda: False,
                    )
                    if cleanup_lease is None:
                        raise WorktreeError(
                            "Could not acquire repository lock for worktree cleanup"
                        )

                    merged = False
                    if (request.delivery_mode == "auto_merge" and final_commit is not None
                            and pending_update and pending_update.get("status") == "completed"):
                        try:
                            merge_commit = self.worktree_manager.merge_into_source(
                                task_worktree
                            )
                        except WorktreeError as error:
                            merge_error = f"{type(error).__name__}: {error}"
                            self._update(
                                task_id,
                                worktree_merged=False,
                                worktree_merge_error=merge_error,
                            )
                            if (
                                pending_update is not None
                                and pending_update.get("status") == "completed"
                            ):
                                pending_update["status"] = "merge_conflict"
                                pending_update["error"] = merge_error
                        else:
                            merged = True
                            self._update(
                                task_id,
                                worktree_merged=True,
                                worktree_merge_commit=merge_commit,
                                worktree_merge_error=None,
                            )

                    # Failed/cancelled/unfinished tasks may contain the only
                    # copy of uncommitted work. Keep their checkout and branch.
                    if (request.delivery_mode == "auto_merge" and pending_update
                            and pending_update.get("status") in {"completed", "merge_conflict"}):
                        self.worktree_manager.remove(
                            task_worktree,
                            force=True,
                            delete_branch=final_commit is None or merged,
                        )
                        self._update(task_id, worktree_cleaned=True, worktree_cleanup_error=None)
                    else:
                        self._update(task_id, worktree_cleaned=False, worktree_cleanup_error=None)
                except Exception as error:  # noqa: BLE001 - preserve task result.
                    logger.warning(
                        "Task worktree cleanup failed for %s: %s",
                        task_id,
                        error,
                    )
                    self._update(
                        task_id,
                        worktree_cleaned=False,
                        worktree_cleanup_error=f"{type(error).__name__}: {error}",
                    )
                finally:
                    if cleanup_lease is not None:
                        try:
                            cleanup_lease.release()
                        except WorkspaceLockError as error:
                            logger.warning(
                                "Worktree cleanup lock release failed: %s",
                                error,
                            )

        if pending_update is not None:
            self._update(task_id, **pending_update)

    def _run_project_check(self, task_id: str, workspace: Path, cancel_event: threading.Event) -> None:
        """Run frozen admin checks only, never the agent or candidate delivery."""
        lease = None
        outcome = None
        report = None
        try:
            record = self.get(task_id)
            if record is None or record["status"] in TERMINAL_STATUSES:
                return
            profile = RepairProfile(**record["project_check_profile"])
            source = self._workspace(profile.workspace)
            if source != workspace or str(source) != record["source_workspace"]:
                raise ValueError("Project-check workspace does not match the frozen administrator profile")
            deadline = time.monotonic() + profile.deadline_seconds

            def stopped() -> bool:
                return (self._is_cancelled(task_id, cancel_event) or time.monotonic() >= deadline
                        or (lease is not None and lease.lost))

            self._update(task_id, status="running", started_at=_now())
            lease = self.workspace_lock.acquire(source, cancelled=stopped)
            if lease is None:
                outcome = {"status": "cancelled" if self._is_cancelled(task_id, cancel_event) else "error",
                           "error": "Project check stopped while waiting for the repository lock"}
                return
            if self.worktree_manager.repo_root(source) != source or not self.worktree_enabled:
                raise WorktreeError("Project checks require an enabled worktree and a Git repository root")
            before = inspect_project(source)
            if before["state"] != "not_configured":
                report = before
            elif stopped():
                report = before
                report.update(state="cancelled", summary="检查已停止，未执行测试。")
            else:
                worktree = self.worktree_manager.create(source, task_id)
                self._update(task_id, execution_workspace=str(worktree.workspace), worktree_path=str(worktree.path),
                             worktree_branch=worktree.branch, worktree_source_branch=worktree.source_branch,
                             worktree_base_commit=worktree.base_commit, worktree_cleaned=False)
                if lease.lost:
                    raise WorkspaceLockError("Workspace lock lease was lost before project checks")
                report = inspect_project(worktree.workspace, profile.reproduce_commands,
                                         timeout=profile.command_timeout, environment_config=self.environment_config,
                                         cancelled=stopped)
                after = inspect_project(source)
                original_before, original_after = before["source_before"], after["source_before"]
                report.update(source_repository_before=original_before, source_repository_after=original_after,
                              source_repository_observations_unchanged=(original_before == original_after
                                                                        if original_after is not None else None),
                              execution_mode="managed_worktree",
                              executor=self.environment_config.get("environment_class", "local"))
                report["limitations"].append(
                    "Managed worktrees share Git metadata and are not OS sandboxes; source observations are not a security guarantee.")
                if original_after is None:
                    report.update(state="error", summary="无法检查源仓库的执行后状态，不能认定接入检查通过。")
                elif original_before != original_after:
                    report.update(state="source_changed", summary="源仓库在检查期间发生变化；请人工检查，未自动清理或回滚。")
            if self._is_cancelled(task_id, cancel_event):
                report.update(state="cancelled", summary="接入检查已取消，不能认定检查通过。")
            elif time.monotonic() >= deadline:
                report.update(state="error", summary="接入检查达到管理员配置的总预算，不能认定检查通过。")
            if lease.lost:
                raise WorkspaceLockError("Workspace lock lease was lost during project checks")
            outcome = {"status": report["state"], "preflight_report": report,
                       "baseline": report["baseline"], "error": report["summary"] if report["state"] == "error" else None}
        except Exception as error:  # noqa: BLE001 - background failures become task state.
            if report is not None:
                report.update(state="error", summary="接入检查执行异常，不能认定检查通过。", error_type=type(error).__name__)
            outcome = {"status": "error", "preflight_report": report,
                       "error": f"{type(error).__name__}: {error}"}
        finally:
            if lease is not None:
                try:
                    lease.release()
                    if lease.lost:
                        raise WorkspaceLockError("Workspace lock lease was lost")
                except Exception as error:  # noqa: BLE001 - never publish a pass without ownership.
                    if report is not None:
                        report.update(state="error", summary="仓库锁失效，不能认定接入检查通过。")
                    outcome = {"status": "error", "preflight_report": report,
                               "error": f"{type(error).__name__}: {error}"}
            if outcome is not None:
                if report is not None:
                    outcome.update(preflight_report=report, baseline=report["baseline"])
                self._update(task_id, **outcome, finished_at=_now())

    def _mark_lock_failure(self, task_id: str, message: str) -> None:
        record = self.get(task_id)
        if record is not None and record["status"] not in {"error", "cancelled"}:
            self._update(task_id, status="error", error=message, finished_at=_now())

    def _update(self, task_id: str, **changes) -> None:
        self.task_store.update(task_id, changes)

    def get(self, task_id: str) -> dict | None:
        return self.task_store.get(task_id)

    def list_tasks(self, *, limit: int = 20, cursor: str | None = None, task_status: str | None = None,
                   kind: str | None = None, workspace: str | None = None) -> dict:
        if task_status is not None and task_status not in TERMINAL_STATUSES | {"queued", "running"}:
            raise ValueError("Unknown task status")
        if kind is not None and kind not in {"ci_repair", "task", "project_check"}:
            raise ValueError("Unknown task kind")
        selected_workspace = str(self._workspace(workspace)) if workspace is not None else None
        return self.task_store.list_tasks(limit=limit, cursor=cursor, status=task_status, kind=kind,
                                          workspace=selected_workspace, workspace_root=str(self.workspace_root))

    def candidate(self, task_id: str) -> dict:
        record = self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        if not record.get("candidate"):
            raise WorktreeError("Task has no reviewable candidate")
        return record["candidate"]

    def export_candidate(self, task_id: str, request: ReviewRequest) -> dict:
        record = self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        return candidate_export(record, commit=request.commit, diff_sha256=request.diff_sha256)

    def _review_worktree(self, record: dict) -> TaskWorktree:
        source = self._workspace(record["source_workspace"])
        root = self.worktree_manager.repo_root(source)
        path = Path(record["worktree_path"]).resolve()
        if root is None or self.worktree_manager.root not in path.parents:
            raise WorktreeError("Review worktree is missing or outside the managed root")
        return TaskWorktree(source, root, path, Path(record["execution_workspace"]),
                            record["worktree_branch"], record["worktree_base_commit"], record["worktree_source_branch"])

    def review(self, task_id: str, request: ReviewRequest, *, approve: bool) -> dict:
        record = self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        worktree = self._review_worktree(record) if record.get("worktree_path") else None
        if worktree is None:
            raise WorktreeError("Task has no managed candidate")
        lease = self.workspace_lock.acquire(worktree.repo_root, cancelled=lambda: False)
        if lease is None:
            raise WorktreeError("Could not acquire review lock")
        try:
            record = self.get(task_id)
            candidate = record.get("candidate") or {}
            if request.commit != candidate.get("commit") or request.diff_sha256 != candidate.get("diff_sha256"):
                raise WorktreeError("Review must reference the exact candidate commit and diff hash")
            decision = "approved" if approve else "rejected"
            if record["status"] == decision:
                return record
            if record["status"] != "awaiting_review":
                raise WorktreeError("Task is not awaiting review")
            if approve:
                self.worktree_manager.assert_reviewable(worktree, request.commit)
                current = self.worktree_manager.candidate(worktree, request.commit)
                if current != candidate:
                    raise WorktreeError("Candidate metadata changed")
                profile_data = record.get("repair_profile")
                profile = RepairProfile(**profile_data) if profile_data else None
                commands = profile.verification_commands if profile else record["request"].get("verification_commands") or []
                timeout = profile.command_timeout if profile else self.environment_config.get("timeout", 30)
                verification = run_checks(worktree.workspace, commands, self.environment_config,
                                          timeout=timeout, cancelled=lambda: lease.lost)
                self._update(task_id, approval_verification=verification)
                if commands and verification["state"] != "passed":
                    raise WorktreeError("Approval verification did not pass")
                if profile:
                    scope_error = profile.check_scope(current["changed_paths"])
                    protection_error = ProtectedFiles(worktree.workspace, profile.protected_paths).check(
                        record.get("repair_protected_baseline", {}))
                    if scope_error or protection_error:
                        raise WorktreeError(scope_error or protection_error)
                self.worktree_manager.assert_reviewable(worktree, request.commit)
                if lease.lost:
                    raise WorktreeError("Review lock lease was lost")
                merge_commit = self.worktree_manager.approve_candidate(worktree, request.commit)
                self._update(task_id, worktree_merged=True, worktree_merge_commit=merge_commit)
            self._update(task_id, status=decision,
                         review={"decision": decision, "at": _now(), "reason": request.reason,
                                 "commit": request.commit, "diff_sha256": request.diff_sha256})
            if approve:
                self.memory.append(record["session_id"], worktree.source_workspace,
                                   MemoryTurn(record["task"], record["result"]["answer"], _now()))
            return self.get(task_id)
        finally:
            lease.release()

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
        repair_profiles=load_profiles(os.getenv("REPO_AGENT_REPAIR_PROFILES")),
        max_pending=int(os.getenv("REPO_AGENT_MAX_PENDING", "100")),
    )
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        manager.close()

    application = FastAPI(title="Repo Agent API", version=__version__, lifespan=lifespan)
    application.state.task_manager = manager
    api_token = os.getenv("REPO_AGENT_API_TOKEN")
    allow_general_tasks = os.getenv("REPO_AGENT_ALLOW_GENERAL_TASKS", "false" if manager.repair_profiles else "true").lower() in {
        "1", "true", "yes",
    }
    if api_token:
        application.add_middleware(TokenAuthMiddleware, token=api_token)

    @application.get("/", response_class=HTMLResponse)
    async def repair_console() -> HTMLResponse:
        return HTMLResponse(files("repo_agent").joinpath("static/repairs.html").read_text(encoding="utf-8"))

    @application.get("/repair-profiles")
    async def repair_profiles() -> dict:
        return {"profiles": [profile.serialize() for profile in manager.repair_profiles.values()]}

    @application.post("/project-checks", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED)
    async def create_project_check(request: ProjectCheckRequest, response: Response) -> dict:
        try:
            record = manager.submit_project_check(request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except TaskQueueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        response.headers["Location"] = f"/tasks/{record['id']}"
        response.headers["Cache-Control"] = "no-store"
        return record

    @application.post("/repairs", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED)
    async def create_repair(request: RepairRequest, response: Response) -> dict:
        try:
            record = manager.submit_repair(request)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except TaskQueueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        response.headers["Location"] = f"/tasks/{record['id']}"
        return record

    @application.get("/tasks/{task_id}/candidate")
    async def get_candidate(task_id: str) -> dict:
        try:
            return manager.candidate(task_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Task not found") from error
        except WorktreeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    async def download_candidate(task_id: str, commit: str, diff_sha256: str, *, patch: bool) -> Response:
        try:
            artifact = await asyncio.to_thread(manager.export_candidate, task_id,
                                              ReviewRequest(commit=commit, diff_sha256=diff_sha256))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Task not found") from error
        except WorktreeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        extension = "patch" if patch else "review.json"
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                   "Content-Disposition": f'attachment; filename="repo-agent-{commit[:12]}.{extension}"',
                   "X-Repo-Agent-Commit": commit, "X-Repo-Agent-Diff-SHA256": diff_sha256}
        if patch:
            return Response(artifact["patch"], media_type="application/octet-stream", headers=headers)
        return JSONResponse(artifact["report"], headers=headers)

    @application.get("/tasks/{task_id}/candidate.patch")
    async def download_patch(task_id: str, commit: Annotated[str, Query(pattern=r"^[0-9a-f]{40,64}$")],
                             diff_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")]) -> Response:
        return await download_candidate(task_id, commit, diff_sha256, patch=True)

    @application.get("/tasks/{task_id}/candidate-report.json")
    async def download_report(task_id: str, commit: Annotated[str, Query(pattern=r"^[0-9a-f]{40,64}$")],
                              diff_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")]) -> Response:
        return await download_candidate(task_id, commit, diff_sha256, patch=False)

    async def review_task(task_id: str, request: ReviewRequest, approve: bool) -> dict:
        try:
            return await asyncio.to_thread(manager.review, task_id, request, approve=approve)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Task not found") from error
        except (WorktreeError, WorkspaceLockError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post("/tasks/{task_id}/approve")
    async def approve_task(task_id: str, request: ReviewRequest) -> dict:
        return await review_task(task_id, request, True)

    @application.post("/tasks/{task_id}/reject")
    async def reject_task(task_id: str, request: ReviewRequest) -> dict:
        return await review_task(task_id, request, False)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/tasks")
    async def list_tasks(response: Response, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                         cursor: Annotated[str | None, Query(max_length=256)] = None,
                         status: str | None = None, kind: Literal["ci_repair", "task", "project_check"] | None = None,
                         workspace: str | None = None) -> dict:
        try:
            response.headers["Cache-Control"] = "no-store"
            return await asyncio.to_thread(manager.list_tasks, limit=limit, cursor=cursor,
                                           task_status=status, kind=kind, workspace=workspace)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @application.post("/tasks", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED)
    async def create_task(request: TaskRequest, response: Response) -> dict:
        if not allow_general_tasks:
            raise HTTPException(status_code=403, detail="General tasks disabled; use an administrator-configured repair profile")
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
        host=os.getenv("REPO_AGENT_HOST", "127.0.0.1"),
        port=int(os.getenv("REPO_AGENT_PORT", "8000")),
    )
