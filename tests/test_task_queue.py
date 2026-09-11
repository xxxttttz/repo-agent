import time

from repo_agent.api import TaskManager, TaskRequest
from repo_agent.task_queue import QueuedTask, RedisTaskQueue
from repo_agent.task_store import InMemoryTaskStore


class FakeRedis:
    def __init__(self):
        self.messages = []
        self.claimed = []
        self.acked = []
        self.deleted = []
        self.touched = []

    def xgroup_create(self, stream, group, id, mkstream):
        assert (stream, group, id, mkstream) == (
            "repo-agent:tasks", "repo-agent-workers", "0", True
        )

    def xadd(self, stream, fields):
        message_id = f"{len(self.messages) + 1}-0"
        self.messages.append((message_id, fields))
        return message_id

    def xautoclaim(self, stream, group, consumer, idle, cursor, count):
        if self.claimed:
            return ("0-0", [self.claimed.pop(0)], [])
        return ("0-0", [], [])

    def xreadgroup(self, group, consumer, streams, count, block):
        if not self.messages:
            return []
        return [("repo-agent:tasks", [self.messages.pop(0)])]

    def xack(self, stream, group, message_id):
        self.acked.append(message_id)

    def xdel(self, stream, message_id):
        self.deleted.append(message_id)

    def xclaim(self, stream, group, consumer, min_idle_time, message_ids, justid):
        self.touched.extend(message_ids)
        return message_ids


class FakeQueue:
    def __init__(self):
        self.messages = []
        self.acked = []

    def publish(self, payload):
        message_id = f"{len(self.messages) + 1}-0"
        self.messages.append(QueuedTask(message_id, payload))
        return message_id

    def read(self, *, block_ms=1_000):
        if self.messages:
            return self.messages.pop(0)
        time.sleep(0.001)
        return None

    def acknowledge(self, message_id):
        self.acked.append(message_id)

    def touch(self, message_ids):
        pass


def test_redis_stream_queue_publish_consume_touch_and_acknowledge():
    client = FakeRedis()
    queue = RedisTaskQueue("redis://unused", client=client, consumer="worker-one")

    message_id = queue.publish({"task_id": "task-one"})
    queued = queue.read(block_ms=1)
    queue.touch([message_id])
    queue.acknowledge(message_id)

    assert queued == QueuedTask("1-0", {"task_id": "task-one"})
    assert client.touched == ["1-0"]
    assert client.acked == ["1-0"]
    assert client.deleted == ["1-0"]


def test_redis_stream_queue_recovers_claimed_message():
    client = FakeRedis()
    client.claimed.append(("old-0", {"payload": '{"task_id":"recovered"}'}))
    queue = RedisTaskQueue("redis://unused", client=client)

    assert queue.read(block_ms=1) == QueuedTask("old-0", {"task_id": "recovered"})


def test_task_manager_executes_and_acknowledges_queued_task(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    queue = FakeQueue()
    manager = TaskManager(
        tmp_path,
        redis_url=None,
        task_store=InMemoryTaskStore(),
        task_queue=queue,
    )
    record = manager.submit(TaskRequest(task="Inspect project", provider="mock", max_steps=2))

    for _ in range(300):
        result = manager.get(record["id"])
        if result["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("queued task did not finish")

    assert result["status"] == "completed"
    assert queue.acked == ["1-0"]
    manager.close()
