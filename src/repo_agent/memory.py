"""Conversation memory backends for related API tasks."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryTurn:
    task: str
    answer: str
    created_at: str


class SessionMemory(Protocol):
    def load(self, session_id: str, workspace: Path) -> list[MemoryTurn]: ...

    def append(self, session_id: str, workspace: Path, turn: MemoryTurn) -> None: ...


class RedisSessionMemory:
    """Keep bounded conversation turns in Redis lists with sliding expiry."""

    def __init__(
        self,
        url: str,
        *,
        namespace: str = "repo-agent:session",
        ttl_seconds: int = 86_400,
        max_turns: int = 8,
        client=None,
    ):
        if ttl_seconds < 1 or max_turns < 1:
            raise ValueError("ttl_seconds and max_turns must be at least 1")
        if client is None:
            try:
                from redis import Redis
            except ImportError as error:
                raise RuntimeError(
                    "Redis memory requires the 'service' extra: pip install 'repo-agent[service]'"
                ) from error
            client = Redis.from_url(url, decode_responses=True)
        self.client = client
        self.namespace = namespace.rstrip(":")
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns

    def _key(self, session_id: str, workspace: Path) -> str:
        workspace_hash = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
        return f"{self.namespace}:{workspace_hash}:{session_id}"

    def load(self, session_id: str, workspace: Path) -> list[MemoryTurn]:
        try:
            values = self.client.lrange(self._key(session_id, workspace), 0, -1)
            turns = []
            for value in values:
                item = json.loads(value)
                turns.append(MemoryTurn(
                    task=str(item["task"]),
                    answer=str(item["answer"]),
                    created_at=str(item["created_at"]),
                ))
            return turns
        except Exception as error:  # noqa: BLE001 - memory is optional context.
            logger.warning("Redis session memory read failed; continuing without history: %s", error)
            return []

    def append(self, session_id: str, workspace: Path, turn: MemoryTurn) -> None:
        payload = json.dumps(asdict(turn), ensure_ascii=False, separators=(",", ":"))
        key = self._key(session_id, workspace)
        try:
            pipeline = self.client.pipeline(transaction=True)
            pipeline.rpush(key, payload)
            pipeline.ltrim(key, -self.max_turns, -1)
            pipeline.expire(key, self.ttl_seconds)
            pipeline.execute()
        except Exception as error:  # noqa: BLE001 - memory must not fail a task.
            logger.warning("Redis session memory write failed; result was not remembered: %s", error)


class InMemorySessionMemory:
    """Development fallback when the API runs without Redis."""

    def __init__(self, *, ttl_seconds: int = 86_400, max_turns: int = 8):
        if ttl_seconds < 1 or max_turns < 1:
            raise ValueError("ttl_seconds and max_turns must be at least 1")
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns
        self._values: dict[tuple[str, str], tuple[float, list[MemoryTurn]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(session_id: str, workspace: Path) -> tuple[str, str]:
        return session_id, str(workspace.resolve())

    def load(self, session_id: str, workspace: Path) -> list[MemoryTurn]:
        key = self._key(session_id, workspace)
        with self._lock:
            value = self._values.get(key)
            if value is None:
                return []
            expires_at, turns = value
            if expires_at <= time.monotonic():
                self._values.pop(key, None)
                return []
            return list(turns)

    def append(self, session_id: str, workspace: Path, turn: MemoryTurn) -> None:
        key = self._key(session_id, workspace)
        with self._lock:
            existing = self._values.get(key)
            turns = list(existing[1]) if existing and existing[0] > time.monotonic() else []
            turns.append(turn)
            self._values[key] = (
                time.monotonic() + self.ttl_seconds,
                turns[-self.max_turns:],
            )


def format_memory(turns: list[MemoryTurn], *, max_chars: int = 12_000) -> str:
    """Format the newest completed turns within a bounded context budget."""
    selected: list[str] = []
    used = 0
    for turn in reversed(turns):
        block = f"User task: {turn.task}\nAgent answer: {turn.answer}"
        if selected and used + len(block) > max_chars:
            break
        selected.append(block[:max_chars])
        used += len(block)
    selected.reverse()
    return "\n\n".join(selected)
