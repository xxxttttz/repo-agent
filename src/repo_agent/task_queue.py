"""Redis Streams task queue with acknowledgement and stale-task recovery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4


class TaskQueueError(RuntimeError):
    """Raised when a durable queue operation cannot be completed."""


@dataclass(frozen=True, slots=True)
class QueuedTask:
    message_id: str
    payload: dict


class TaskQueue(Protocol):
    def publish(self, payload: dict) -> str: ...

    def read(self, *, block_ms: int = 1_000) -> QueuedTask | None: ...

    def acknowledge(self, message_id: str) -> None: ...

    def touch(self, message_ids: list[str]) -> None: ...


class RedisTaskQueue:
    """At-least-once task delivery backed by a Redis consumer group."""

    def __init__(
        self,
        url: str,
        *,
        stream: str = "repo-agent:tasks",
        group: str = "repo-agent-workers",
        consumer: str | None = None,
        reclaim_after_ms: int = 30_000,
        client=None,
    ):
        if reclaim_after_ms < 1:
            raise ValueError("reclaim_after_ms must be at least 1")
        if client is None:
            try:
                from redis import Redis
            except ImportError as error:
                raise RuntimeError(
                    "Redis task queues require the 'service' extra: "
                    "pip install 'repo-agent[service]'"
                ) from error
            client = Redis.from_url(url, decode_responses=True)
        self.client = client
        self.stream = stream
        self.group = group
        self.consumer = consumer or uuid4().hex
        self.reclaim_after_ms = reclaim_after_ms
        self._claim_cursor = "0-0"
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as error:
            if "BUSYGROUP" not in str(error):
                raise TaskQueueError(f"Could not initialize Redis task queue: {error}") from error

    def publish(self, payload: dict) -> str:
        try:
            return str(self.client.xadd(
                self.stream,
                {"payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
            ))
        except Exception as error:
            raise TaskQueueError(f"Could not enqueue task: {error}") from error

    def read(self, *, block_ms: int = 1_000) -> QueuedTask | None:
        try:
            claimed = self.client.xautoclaim(
                self.stream,
                self.group,
                self.consumer,
                self.reclaim_after_ms,
                self._claim_cursor,
                count=1,
            )
            self._claim_cursor, messages = str(claimed[0]), claimed[1]
            if messages:
                return self._decode(messages[0])

            response = self.client.xreadgroup(
                self.group,
                self.consumer,
                {self.stream: ">"},
                count=1,
                block=block_ms,
            )
            if response and response[0][1]:
                return self._decode(response[0][1][0])
            return None
        except TaskQueueError:
            raise
        except Exception as error:
            raise TaskQueueError(f"Could not consume task: {error}") from error

    def acknowledge(self, message_id: str) -> None:
        try:
            self.client.xack(self.stream, self.group, message_id)
            self.client.xdel(self.stream, message_id)
        except Exception as error:
            raise TaskQueueError(f"Could not acknowledge task: {error}") from error

    def touch(self, message_ids: list[str]) -> None:
        if not message_ids:
            return
        try:
            self.client.xclaim(
                self.stream,
                self.group,
                self.consumer,
                min_idle_time=0,
                message_ids=message_ids,
                justid=True,
            )
        except Exception as error:
            raise TaskQueueError(f"Could not refresh task ownership: {error}") from error

    @staticmethod
    def _decode(message: tuple[str, dict]) -> QueuedTask:
        message_id, fields = message
        try:
            payload = json.loads(fields["payload"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise TaskQueueError(f"Invalid queued task {message_id}: {error}") from error
        if not isinstance(payload, dict):
            raise TaskQueueError(f"Invalid queued task {message_id}: payload is not an object")
        return QueuedTask(str(message_id), payload)
