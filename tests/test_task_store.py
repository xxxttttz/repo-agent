import json

import pytest

from repo_agent.task_store import InMemoryTaskStore, RedisTaskStore


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiry = {}
        self.sorted_sets = {}

    def pipeline(self, transaction=True):
        assert isinstance(transaction, bool)
        return FakePipeline(self)

    def hgetall(self, key):
        return self.values.get(key, {})

    def exists(self, key):
        return key in self.values

    def hmget(self, key, fields):
        return [self.values.get(key, {}).get(field) for field in fields]

    def zrevrangebylex(self, key, maximum, minimum, start=0, num=None):
        assert minimum == "-"
        members = sorted(self.sorted_sets.get(key, {}), reverse=True)
        if maximum != "+":
            assert maximum.startswith("(")
            members = [member for member in members if member < maximum[1:]]
        return members[start:start + num] if num is not None else members[start:]

    def zrem(self, key, member):
        self.sorted_sets.get(key, {}).pop(member, None)


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.operations = []

    def hset(self, key, mapping):
        self.operations.append(("hset", key, mapping))
        return self

    def expire(self, key, ttl):
        self.operations.append(("expire", key, ttl))
        return self

    def zadd(self, key, mapping):
        self.operations.append(("zadd", key, mapping))
        return self

    def zremrangebyrank(self, key, start, stop):
        self.operations.append(("zremrangebyrank", key, (start, stop)))
        return self

    def hmget(self, key, fields):
        self.operations.append(("hmget", key, fields))
        return self

    def execute(self):
        results = []
        for operation, key, value in self.operations:
            if operation == "hset":
                self.client.values.setdefault(key, {}).update(value)
            elif operation == "expire":
                self.client.expiry[key] = value
            elif operation == "zadd":
                self.client.sorted_sets.setdefault(key, {}).update(value)
            elif operation == "zremrangebyrank":
                members = sorted(self.client.sorted_sets.get(key, {}))
                start, stop = value
                stop = len(members) + stop if stop < 0 else stop
                for member in members[start:stop + 1] if stop >= start else []:
                    self.client.zrem(key, member)
            elif operation == "hmget":
                results.append(self.client.hmget(key, value))
        return results


def test_in_memory_task_store_returns_independent_snapshots():
    store = InMemoryTaskStore()
    record = {"id": "one", "status": "queued", "result": None}
    store.put(record)
    record["status"] = "changed-outside"

    updated = store.update("one", {"status": "running"})
    updated["status"] = "changed-again"

    assert store.get("one")["status"] == "running"
    assert store.get("missing") is None


def test_redis_task_store_persists_and_can_be_reloaded():
    client = FakeRedis()
    first = RedisTaskStore("redis://unused", client=client, ttl_seconds=30)
    first.put({"id": "one", "status": "queued", "result": None})
    first.update("one", {"status": "completed", "result": {"answer": "done"}})

    second = RedisTaskStore("redis://unused", client=client, ttl_seconds=30)
    record = second.get("one")

    assert record["status"] == "completed"
    assert record["result"]["answer"] == "done"
    assert json.loads(client.values["repo-agent:task:one"]["status"]) == "completed"
    assert client.expiry["repo-agent:task:one"] == 30


def history_record(identifier, second=1, **overrides):
    return {"id": identifier, "created_at": f"2026-10-04T10:00:{second:02}.000000+00:00",
            "task": "repair " + identifier, "kind": "ci_repair", "status": "awaiting_review",
            "source_workspace": "/workspaces/project", "profile_id": "python",
            "failure_log": "sensitive log", "candidate": {"diff": "full patch"},
            "result": {"messages": ["large trajectory"]}, **overrides}


@pytest.fixture(params=["memory", "redis"])
def history_store(request):
    return InMemoryTaskStore() if request.param == "memory" else RedisTaskStore("redis://unused", client=FakeRedis())


def test_history_returns_only_summaries_and_deterministic_newest_first(history_store):
    for identifier in ["a", "c", "b"]:
        history_store.put(history_record(identifier))
    first = history_store.list_tasks(limit=2)
    assert [record["id"] for record in first["tasks"]] == ["c", "b"]
    assert first["next_cursor"]
    assert not first["degraded"]
    assert "failure_log" not in first["tasks"][0]
    assert "candidate" not in first["tasks"][0]
    assert "result" not in first["tasks"][0]
    second = history_store.list_tasks(limit=2, cursor=first["next_cursor"])
    assert [record["id"] for record in second["tasks"]] == ["a"]
    assert second["next_cursor"] is None
    first["tasks"][0]["status"] = "changed outside"
    assert history_store.get("c")["status"] == "awaiting_review"


def test_history_cursor_survives_new_submissions_and_state_changes(history_store):
    for identifier, second in [("a", 1), ("b", 2), ("c", 3)]:
        history_store.put(history_record(identifier, second))
    first = history_store.list_tasks(limit=1)
    assert first["tasks"][0]["id"] == "c"
    history_store.put(history_record("d", 4))
    history_store.update("b", {"status": "approved"})
    second = history_store.list_tasks(limit=2, cursor=first["next_cursor"])
    assert [record["id"] for record in second["tasks"]] == ["b", "a"]
    assert second["tasks"][0]["status"] == "approved"


def test_history_normalizes_timezones_for_stable_order(history_store):
    history_store.put(history_record("a", created_at="2026-10-04T10:00:01.000000+00:00"))
    history_store.put(history_record("b", created_at="2026-10-04T18:00:01.000000+08:00"))
    first = history_store.list_tasks(limit=1)
    assert first["tasks"][0]["id"] == "b"
    second = history_store.list_tasks(limit=1, cursor=first["next_cursor"])
    assert second["tasks"][0]["id"] == "a"


def test_history_filters_and_respects_workspace_boundary(history_store):
    history_store.put(history_record("a", status="approved"))
    history_store.put(history_record("b", kind="task"))
    history_store.put(history_record("c", source_workspace="/workspaces/other"))
    history_store.put(history_record("d", source_workspace="/outside/project"))
    history_store.put(history_record("e", source_workspace="/workspaces/../outside"))
    selected = history_store.list_tasks(status="awaiting_review", kind="ci_repair",
                                         workspace="/workspaces/other", workspace_root="/workspaces")
    assert [record["id"] for record in selected["tasks"]] == ["c"]
    all_rows = history_store.list_tasks(workspace_root="/workspaces")["tasks"]
    assert {row["id"] for row in all_rows} == {"a", "b", "c"}


def test_history_truncates_task_titles_without_changing_saved_task(history_store):
    history_store.put(history_record("long", task="任" * 4000))
    assert history_store.list_tasks()["tasks"][0]["task"] == "任" * 240 + "…"
    assert history_store.get("long")["task"] == "任" * 4000


@pytest.mark.parametrize("cursor", ["", "garbage", "A" * 300, "aW52YWxpZA", "$$$$"])
def test_history_rejects_invalid_cursors(history_store, cursor):
    with pytest.raises(ValueError, match="cursor"):
        history_store.list_tasks(cursor=cursor)


@pytest.mark.parametrize("limit", [0, 101])
def test_history_rejects_invalid_page_sizes(history_store, limit):
    with pytest.raises(ValueError, match="limit"):
        history_store.list_tasks(limit=limit)


def test_redis_history_survives_restart_and_does_not_resurrect_expired_cache():
    client = FakeRedis()
    first = RedisTaskStore("redis://unused", client=client)
    first.put(history_record("a"))
    first.put(history_record("b", 2))
    second = RedisTaskStore("redis://unused", client=client)
    assert [row["id"] for row in second.list_tasks()["tasks"]] == ["b", "a"]
    # Expiration is simulated by removing the Redis hash, leaving a stale index.
    client.values.pop("repo-agent:task:b")
    assert [row["id"] for row in first.list_tasks()["tasks"]] == ["a"]
    assert len(client.sorted_sets[first.history_key]) == 1


def test_redis_history_outage_is_explicitly_degraded(monkeypatch):
    client = FakeRedis()
    store = RedisTaskStore("redis://unused", client=client)
    store.put(history_record("cached"))

    def unavailable(*args, **kwargs):
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr(client, "zrevrangebylex", unavailable)
    page = store.list_tasks()
    assert page["degraded"]
    assert [row["id"] for row in page["tasks"]] == ["cached"]


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_history_index_is_bounded_but_old_known_ids_remain_queryable(backend):
    store = InMemoryTaskStore(history_limit=2) if backend == "memory" else RedisTaskStore(
        "redis://unused", client=FakeRedis(), history_limit=2)
    for identifier, second in [("a", 1), ("b", 2), ("c", 3)]:
        store.put(history_record(identifier, second))
    assert [row["id"] for row in store.list_tasks()["tasks"]] == ["c", "b"]
    assert store.list_tasks()["history_limit"] == 2
    assert store.get("a")["id"] == "a"


def test_redis_filtered_scan_is_bounded_and_can_continue_from_an_empty_page():
    client = FakeRedis()
    store = RedisTaskStore("redis://unused", client=client)
    store.put(history_record("target", 1))
    for number in range(1001):
        store.put(history_record(f"new-{number:04}", 2, status="approved"))
    first = store.list_tasks(status="awaiting_review")
    assert first["tasks"] == []
    assert first["next_cursor"]
    second = store.list_tasks(status="awaiting_review", cursor=first["next_cursor"])
    assert [row["id"] for row in second["tasks"]] == ["target"]
    assert second["next_cursor"] is None
