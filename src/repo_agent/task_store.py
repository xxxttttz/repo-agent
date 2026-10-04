"""Persistent task records used by the HTTP service."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Protocol

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 10_000
_SUMMARY_FIELDS = (
    "id", "task", "status", "kind", "profile_id", "created_at", "started_at", "finished_at",
    "source_workspace", "delivery_mode",
)
_MEMBER_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\|[A-Za-z0-9_-]{1,64}")


def _member(record: dict) -> str:
    created = record.get("created_at")
    try:
        date = datetime.fromisoformat(created) if created else datetime.min.replace(tzinfo=UTC)
        date = date.replace(tzinfo=UTC) if date.tzinfo is None else date.astimezone(UTC)
    except (TypeError, ValueError):
        date = datetime.min.replace(tzinfo=UTC)
    stamp = f"{date.year:04}-{date.month:02}-{date.day:02}T{date.hour:02}:{date.minute:02}:{date.second:02}.{date.microsecond:06}Z"
    return f"{stamp}|{record['id']}"


def _cursor(member: str | None) -> str | None:
    return base64.urlsafe_b64encode(member.encode()).decode().rstrip("=") if member else None


def _after(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 256:
            raise ValueError("Invalid history cursor")
        member = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True).decode()
        if not _MEMBER_PATTERN.fullmatch(member) or _cursor(member) != cursor:
            raise ValueError("Invalid history cursor")
        datetime.fromisoformat(member.split("|", 1)[0])
        return member
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise ValueError("Invalid history cursor") from error


def _summary(record: dict) -> dict:
    result = {name: deepcopy(record.get(name)) for name in _SUMMARY_FIELDS}
    result["kind"] = record.get("kind") or "task"
    title = record.get("task") or ""
    result["task"] = title[:240] + ("…" if len(title) > 240 else "")
    return result


def _matches(record: dict, *, status=None, kind=None, workspace=None, workspace_root=None) -> bool:
    if status is not None and record.get("status") != status:
        return False
    if kind is not None and (record.get("kind") or "task") != kind:
        return False
    source = record.get("source_workspace")
    if workspace is not None and source != workspace:
        return False
    if workspace_root is not None:
        if not source or not PurePath(source).is_absolute():
            return False
        root, path = PurePath(workspace_root), PurePath(source)
        if ".." in path.parts or (path != root and root not in path.parents):
            return False
    return True


class TaskStore(Protocol):
    def put(self, record: dict) -> None: ...

    def get(self, task_id: str) -> dict | None: ...

    def update(self, task_id: str, changes: dict) -> dict | None: ...

    def list_tasks(self, *, limit: int = 20, cursor: str | None = None, status: str | None = None,
                   kind: str | None = None, workspace: str | None = None, workspace_root: str | None = None) -> dict: ...


class InMemoryTaskStore:
    def __init__(self, *, history_limit: int = HISTORY_LIMIT):
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self.history_limit = history_limit
        self._records: dict[str, dict] = {}
        self._lock = threading.Lock()

    def put(self, record: dict) -> None:
        with self._lock:
            self._records[str(record["id"])] = deepcopy(record)

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            record = self._records.get(task_id)
            return deepcopy(record) if record is not None else None

    def update(self, task_id: str, changes: dict) -> dict | None:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return None
            record.update(deepcopy(changes))
            return deepcopy(record)

    def list_tasks(self, *, limit: int = 20, cursor: str | None = None, status: str | None = None,
                   kind: str | None = None, workspace: str | None = None, workspace_root: str | None = None) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("History page limit must be between 1 and 100")
        after = _after(cursor)
        with self._lock:
            rows = sorted(((_member(record), record) for record in self._records.values()),
                          key=lambda row: row[0], reverse=True)[:self.history_limit]
            selected = [(member, _summary(record)) for member, record in rows
                        if (after is None or member < after) and _matches(
                            record, status=status, kind=kind, workspace=workspace, workspace_root=workspace_root)][:limit + 1]
        return {"tasks": [record for _, record in selected[:limit]],
                "next_cursor": _cursor(selected[limit - 1][0]) if len(selected) > limit else None,
                "degraded": False, "history_limit": self.history_limit}


class RedisTaskStore:
    """Store task snapshots in Redis, with a process-local fallback."""

    def __init__(
        self,
        url: str,
        *,
        namespace: str = "repo-agent:task",
        ttl_seconds: int = 604_800,
        history_limit: int = HISTORY_LIMIT,
        client=None,
    ):
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be at least 1")
        if client is None:
            try:
                from redis import Redis
            except ImportError as error:
                raise RuntimeError(
                    "Redis task storage requires the 'service' extra: "
                    "pip install 'repo-agent[service]'"
                ) from error
            client = Redis.from_url(url, decode_responses=True)
        self.client = client
        self.namespace = namespace.rstrip(":")
        self.ttl_seconds = ttl_seconds
        self.fallback = InMemoryTaskStore(history_limit=history_limit)
        self.history_limit = history_limit
        self.history_key = f"{self.namespace}:history"
        self._lock = threading.Lock()

    def _key(self, task_id: str) -> str:
        return f"{self.namespace}:{task_id}"

    def put(self, record: dict) -> None:
        self.fallback.put(record)
        try:
            key = self._key(str(record["id"]))
            pipeline = self.client.pipeline(transaction=True)
            pipeline.hset(key, mapping=self._encode(record))
            pipeline.expire(key, self.ttl_seconds)
            pipeline.zadd(self.history_key, {_member(record): 0})
            pipeline.zremrangebyrank(self.history_key, 0, -self.history_limit - 1)
            pipeline.execute()
        except Exception as error:  # noqa: BLE001 - persistence degrades locally.
            logger.warning("Redis task write failed; retaining local snapshot: %s", error)

    def get(self, task_id: str) -> dict | None:
        try:
            values = self.client.hgetall(self._key(task_id))
            if values:
                record = {key: json.loads(value) for key, value in values.items()}
                self.fallback.put(record)
                return record
        except Exception as error:  # noqa: BLE001 - persistence degrades locally.
            logger.warning("Redis task read failed; using local snapshot: %s", error)
        return self.fallback.get(task_id)

    def update(self, task_id: str, changes: dict) -> dict | None:
        fallback_record = self.fallback.update(task_id, changes)
        with self._lock:
            try:
                key = self._key(task_id)
                if not self.client.exists(key):
                    return fallback_record
                pipeline = self.client.pipeline(transaction=True)
                pipeline.hset(key, mapping=self._encode(changes))
                pipeline.expire(key, self.ttl_seconds)
                pipeline.execute()
            except Exception as error:  # noqa: BLE001 - persistence degrades locally.
                logger.warning("Redis task update failed; retaining local snapshot: %s", error)
                return fallback_record
        return self.get(task_id)

    def list_tasks(self, *, limit: int = 20, cursor: str | None = None, status: str | None = None,
                   kind: str | None = None, workspace: str | None = None, workspace_root: str | None = None) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("History page limit must be between 1 and 100")
        after = _after(cursor)
        try:
            rows, scanned, exhausted = [], 0, False
            # Bound Redis reads even when a rare filter matches no recent tasks.
            # Empty pages may still carry a continuation cursor.
            while scanned < 1000:
                size = min(100, 1000 - scanned)
                members = self.client.zrevrangebylex(self.history_key, f"({after}" if after else "+", "-",
                                                    start=0, num=size)
                if not members:
                    exhausted = True
                    break
                pipeline = self.client.pipeline(transaction=False)
                for member in members:
                    pipeline.hmget(self._key(member.split("|", 1)[1]), _SUMMARY_FIELDS)
                snapshots = pipeline.execute()
                for member, values in zip(members, snapshots, strict=True):
                    scanned += 1
                    after = member
                    if not any(value is not None for value in values):
                        self.client.zrem(self.history_key, member)
                        continue
                    record = {name: json.loads(value) for name, value in zip(_SUMMARY_FIELDS, values, strict=True)
                              if value is not None}
                    if not record.get("id") or _member(record) != member:
                        self.client.zrem(self.history_key, member)
                        continue
                    if _matches(record, status=status, kind=kind, workspace=workspace, workspace_root=workspace_root):
                        rows.append((member, _summary(record)))
                    if len(rows) > limit:
                        return {"tasks": [item for _, item in rows[:limit]], "next_cursor": _cursor(rows[limit - 1][0]),
                                "degraded": False, "history_limit": self.history_limit}
                if len(members) < size:
                    exhausted = True
                    break
            return {"tasks": [item for _, item in rows], "next_cursor": None if exhausted else _cursor(after),
                    "degraded": False, "history_limit": self.history_limit}
        except Exception as error:  # noqa: BLE001 - only expose the local fallback, explicitly marked incomplete.
            logger.warning("Redis history read failed; showing local cached tasks only: %s", error)
            page = self.fallback.list_tasks(limit=limit, cursor=cursor, status=status, kind=kind,
                                            workspace=workspace, workspace_root=workspace_root)
            page["degraded"] = True
            return page

    @staticmethod
    def _encode(values: dict) -> dict[str, str]:
        return {
            str(key): json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            for key, value in values.items()
        }
