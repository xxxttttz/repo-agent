import json

from repo_agent.task_store import InMemoryTaskStore, RedisTaskStore


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiry = {}

    def pipeline(self, transaction=True):
        assert transaction
        return FakePipeline(self)

    def hgetall(self, key):
        return self.values.get(key, {})

    def exists(self, key):
        return key in self.values


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

    def execute(self):
        for operation, key, value in self.operations:
            if operation == "hset":
                self.client.values.setdefault(key, {}).update(value)
            else:
                self.client.expiry[key] = value


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
