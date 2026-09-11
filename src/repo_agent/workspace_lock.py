"""Workspace-scoped execution locks for local and distributed workers."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import uuid4


class WorkspaceLockError(RuntimeError):
    """Raised when a workspace lock cannot be acquired or maintained."""


class WorkspaceLease(Protocol):
    @property
    def lost(self) -> bool: ...

    def release(self) -> None: ...


class WorkspaceLock(Protocol):
    def acquire(
        self,
        workspace: Path,
        *,
        cancelled: Callable[[], bool],
    ) -> WorkspaceLease | None: ...


class _LocalLease:
    def __init__(self, lock: threading.Lock):
        self._lock = lock
        self._released = False

    @property
    def lost(self) -> bool:
        return False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._lock.release()


class InMemoryWorkspaceLock:
    """Serialize tasks targeting one workspace within a process."""

    def __init__(self, *, poll_seconds: float = 0.1):
        self.poll_seconds = poll_seconds
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def acquire(self, workspace: Path, *, cancelled: Callable[[], bool]) -> WorkspaceLease | None:
        key = str(workspace.resolve())
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        while not cancelled():
            if lock.acquire(timeout=self.poll_seconds):
                return _LocalLease(lock)
        return None


class _RedisLease:
    _RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
    _RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

    def __init__(self, client, key: str, token: str, lease_ms: int):
        self.client = client
        self.key = key
        self.token = token
        self.lease_ms = lease_ms
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._heartbeat,
            name="repo-agent-workspace-lock",
            daemon=True,
        )
        self._thread.start()

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def _heartbeat(self) -> None:
        interval = max(self.lease_ms / 3 / 1_000, 0.1)
        while not self._stop.wait(interval):
            try:
                renewed = self.client.eval(
                    self._RENEW_SCRIPT, 1, self.key, self.token, self.lease_ms
                )
                if not renewed:
                    self._lost.set()
                    return
            except Exception:  # noqa: BLE001 - any Redis failure invalidates ownership.
                self._lost.set()
                return

    def release(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(self.lease_ms / 1_000, 0.1))
        try:
            released = self.client.eval(self._RELEASE_SCRIPT, 1, self.key, self.token)
            if not released:
                self._lost.set()
        except Exception as error:
            self._lost.set()
            raise WorkspaceLockError(f"Could not release workspace lock: {error}") from error


class RedisWorkspaceLock:
    """A renewable, ownership-safe Redis lease for each workspace path."""

    def __init__(
        self,
        url: str,
        *,
        namespace: str = "repo-agent:workspace-lock",
        lease_ms: int = 30_000,
        poll_seconds: float = 0.2,
        client=None,
    ):
        if lease_ms < 1_000:
            raise ValueError("lease_ms must be at least 1000")
        if client is None:
            try:
                from redis import Redis
            except ImportError as error:
                raise RuntimeError(
                    "Redis workspace locks require the 'service' extra: "
                    "pip install 'repo-agent[service]'"
                ) from error
            client = Redis.from_url(url, decode_responses=True)
        self.client = client
        self.namespace = namespace.rstrip(":")
        self.lease_ms = lease_ms
        self.poll_seconds = poll_seconds

    def _key(self, workspace: Path) -> str:
        digest = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:24]
        return f"{self.namespace}:{digest}"

    def acquire(self, workspace: Path, *, cancelled: Callable[[], bool]) -> WorkspaceLease | None:
        key = self._key(workspace)
        token = uuid4().hex
        while not cancelled():
            try:
                acquired = self.client.set(key, token, nx=True, px=self.lease_ms)
            except Exception as error:
                raise WorkspaceLockError(f"Could not acquire workspace lock: {error}") from error
            if acquired:
                return _RedisLease(self.client, key, token, self.lease_ms)
            time.sleep(self.poll_seconds)
        return None
