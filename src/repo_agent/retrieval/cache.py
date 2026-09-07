"""Redis-backed cache for content-derived source chunks."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Protocol

from .indexer import Chunk

logger = logging.getLogger(__name__)


class ChunkCache(Protocol):
    """Storage contract used by the indexer."""

    def get(self, key: str, path: str) -> list[Chunk] | None: ...

    def set(self, key: str, chunks: Iterable[Chunk]) -> None: ...


class RedisChunkCache:
    """Store chunks in Redis while treating cache failures as misses.

    Values do not include an absolute workspace path, so identical file content
    can be reused across workspaces without leaking host-specific paths.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        namespace: str = "repo-agent:index",
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
                    "Redis caching requires the 'service' extra: pip install 'repo-agent[service]'"
                ) from error
            client = Redis.from_url(url, decode_responses=True)
        self.client = client
        self.namespace = namespace.rstrip(":")
        self.ttl_seconds = ttl_seconds

    def _key(self, digest: str) -> str:
        return f"{self.namespace}:{digest}"

    def get(self, key: str, path: str) -> list[Chunk] | None:
        try:
            raw = self.client.get(self._key(key))
            if raw is None:
                return None
            items = json.loads(raw)
            if not isinstance(items, list):
                return None
            return [
                Chunk(
                    path=path,
                    name=str(item["name"]),
                    start_line=int(item["start_line"]),
                    end_line=int(item["end_line"]),
                    text=str(item["text"]),
                )
                for item in items
            ]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            logger.warning("Ignoring invalid Redis index cache entry: %s", error)
            return None
        except Exception as error:  # noqa: BLE001 - clients expose different connection errors.
            logger.warning("Redis index cache read failed; rebuilding file: %s", error)
            return None

    def set(self, key: str, chunks: Iterable[Chunk]) -> None:
        payload = json.dumps(
            [
                {
                    "name": chunk.name,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "text": chunk.text,
                }
                for chunk in chunks
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            self.client.set(self._key(key), payload, ex=self.ttl_seconds)
        except Exception as error:  # noqa: BLE001 - clients expose different connection errors.
            logger.warning("Redis index cache write failed; continuing without cache: %s", error)
