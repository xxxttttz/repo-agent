from pathlib import Path

from repo_agent.memory import (
    InMemorySessionMemory,
    MemoryTurn,
    RedisSessionMemory,
    format_memory,
)


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.operations = []

    def rpush(self, key, value):
        self.operations.append(("rpush", key, value))
        return self

    def ltrim(self, key, start, end):
        self.operations.append(("ltrim", key, start, end))
        return self

    def expire(self, key, ttl):
        self.operations.append(("expire", key, ttl))
        return self

    def execute(self):
        for operation in self.operations:
            if operation[0] == "rpush":
                self.client.values.setdefault(operation[1], []).append(operation[2])
            elif operation[0] == "ltrim":
                self.client.values[operation[1]] = self.client.values[operation[1]][operation[2]:]
            elif operation[0] == "expire":
                self.client.expiry[operation[1]] = operation[2]


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.expiry = {}

    def pipeline(self, transaction=True):
        assert transaction
        return FakePipeline(self)

    def lrange(self, key, start, end):
        values = self.values.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]


def test_redis_session_memory_is_bounded_and_workspace_scoped(tmp_path):
    client = FakeRedis()
    memory = RedisSessionMemory("redis://unused", client=client, max_turns=2, ttl_seconds=30)
    first_workspace = tmp_path / "one"
    second_workspace = tmp_path / "two"
    first_workspace.mkdir()
    second_workspace.mkdir()

    for number in range(3):
        memory.append("session", first_workspace, MemoryTurn(f"task {number}", f"answer {number}", "now"))

    assert [turn.task for turn in memory.load("session", first_workspace)] == ["task 1", "task 2"]
    assert memory.load("session", second_workspace) == []
    assert set(client.expiry.values()) == {30}


def test_in_memory_session_and_formatter_keep_recent_context(tmp_path):
    memory = InMemorySessionMemory(max_turns=2)
    workspace = Path(tmp_path)
    memory.append("session", workspace, MemoryTurn("first", "one", "now"))
    memory.append("session", workspace, MemoryTurn("second", "two", "now"))
    memory.append("session", workspace, MemoryTurn("third", "three", "now"))

    turns = memory.load("session", workspace)

    assert [turn.task for turn in turns] == ["second", "third"]
    assert "User task: second" in format_memory(turns)
    assert "Agent answer: three" in format_memory(turns)
