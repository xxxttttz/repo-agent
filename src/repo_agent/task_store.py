"""Persistent task records used by the HTTP service."""

from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from typing import Protocol

logger = logging.getLogger(__name__)


class TaskStore(Protocol):
    def put(self, record: dict) -> None: ...

    def get(self, task_id: str) -> dict | None: ...

    def update(self, task_id: str, changes: dict) -> dict | None: ...


class InMemoryTaskStore:
    def __init__(self):
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


class RedisTaskStore:
    """Store task snapshots in Redis, with a process-local fallback."""

    def __init__(
        self,
        url: str,
        *,
        namespace: str = "repo-agent:task",
        ttl_seconds: int = 604_800,
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
        self.fallback = InMemoryTaskStore()
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

    @staticmethod
    def _encode(values: dict) -> dict[str, str]:
        return {
            str(key): json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            for key, value in values.items()
        }
