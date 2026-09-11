import threading
import time
from types import SimpleNamespace

from repo_agent.api import TaskManager, TaskRequest
from repo_agent.workspace_lock import InMemoryWorkspaceLock, RedisWorkspaceLock


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, nx=False, px=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, script, number_of_keys, key, token, *args):
        assert number_of_keys == 1
        if self.values.get(key) != token:
            return 0
        if "pexpire" in script:
            return 1
        self.values.pop(key, None)
        return 1


def test_in_memory_workspace_lock_serializes_same_workspace(tmp_path):
    locks = InMemoryWorkspaceLock(poll_seconds=0.01)
    first = locks.acquire(tmp_path, cancelled=lambda: False)
    acquired = threading.Event()
    second_lease = []

    def acquire_second():
        second_lease.append(locks.acquire(tmp_path, cancelled=lambda: False))
        acquired.set()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    assert not acquired.wait(0.05)
    first.release()
    assert acquired.wait(0.5)
    second_lease[0].release()
    thread.join(timeout=1)


def test_redis_workspace_lock_only_releases_its_own_token(tmp_path):
    client = FakeRedis()
    locks = RedisWorkspaceLock(
        "redis://unused", client=client, lease_ms=1_000, poll_seconds=0.01
    )
    lease = locks.acquire(tmp_path, cancelled=lambda: False)
    key = locks._key(tmp_path)
    client.values[key] = "new-owner"

    lease.release()

    assert client.values[key] == "new-owner"


def test_task_manager_serializes_tasks_for_same_workspace(tmp_path, monkeypatch):
    state_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def controlled_execute(spec, *, cache=None):
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.05)
        with state_lock:
            active -= 1
        return SimpleNamespace(
            trajectory={"status": "completed", "answer": "done", "messages": [], "steps": []},
            index={"files": 0, "chunks": 0, "cache_hits": 0, "cache_misses": 0},
        )

    monkeypatch.setattr("repo_agent.api.execute_task", controlled_execute)
    manager = TaskManager(tmp_path, redis_url=None, workers=2)
    first = manager.submit(TaskRequest(task="first", provider="mock"))
    second = manager.submit(TaskRequest(task="second", provider="mock"))

    for _ in range(300):
        records = [manager.get(first["id"]), manager.get(second["id"])]
        if all(record["status"] == "completed" for record in records):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("tasks did not finish")

    assert maximum_active == 1
    manager.close()
