from repo_agent.retrieval import (
    BM25Retriever,
    Chunk,
    IndexStats,
    RedisChunkCache,
    build_index,
    format_results,
)


class MemoryCache:
    def __init__(self):
        self.values = {}
        self.set_calls = 0

    def get(self, key, path):
        chunks = self.values.get(key)
        if chunks is None:
            return None
        return [Chunk(path, chunk.name, chunk.start_line, chunk.end_line, chunk.text) for chunk in chunks]

    def set(self, key, chunks):
        self.set_calls += 1
        self.values[key] = list(chunks)


def test_build_index_chunks_python_definitions_and_text_files(tmp_path):
    (tmp_path / "app.py").write_text(
        "def alpha():\n    return 'needle'\n\nclass Beta:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "notes.md").write_text("project notes\n", encoding="utf-8")

    chunks = build_index(str(tmp_path))

    assert [(chunk.path, chunk.name) for chunk in chunks] == [
        ("app.py", "alpha"),
        ("app.py", "Beta"),
        ("notes.md", "lines 1-1"),
    ]
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 2


def test_build_index_skips_hidden_and_cache_directories(tmp_path):
    (tmp_path / "visible.py").write_text("value = 1\n", encoding="utf-8")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.py").write_text("secret = 1\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "cached.py").write_text("cached = 1\n", encoding="utf-8")

    chunks = build_index(str(tmp_path))

    assert [chunk.path for chunk in chunks] == ["visible.py"]


def test_build_index_skips_symlinked_files(tmp_path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("outside = 1\n", encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(outside)

    assert build_index(str(tmp_path)) == []


def test_invalid_python_falls_back_to_line_chunks(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    chunks = build_index(str(tmp_path))

    assert len(chunks) == 1
    assert chunks[0].name == "lines 1-2"


def test_build_index_reuses_content_hash_cache_and_invalidates_changed_file(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    cache = MemoryCache()
    first_stats = IndexStats()
    second_stats = IndexStats()

    first = build_index(str(tmp_path), cache=cache, stats=first_stats)
    second = build_index(str(tmp_path), cache=cache, stats=second_stats)

    assert second == first
    assert first_stats.cache_misses == 1
    assert second_stats.cache_hits == 1
    assert cache.set_calls == 1

    source.write_text("def beta():\n    return 2\n", encoding="utf-8")
    changed_stats = IndexStats()
    changed = build_index(str(tmp_path), cache=cache, stats=changed_stats)
    assert changed[0].name == "beta"
    assert changed_stats.cache_misses == 1
    assert cache.set_calls == 2


def test_redis_chunk_cache_serializes_chunks_without_workspace_path():
    class FakeRedis:
        def __init__(self):
            self.values = {}

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, ex):
            self.values[key] = value
            self.expiry = ex

    client = FakeRedis()
    cache = RedisChunkCache(client=client, ttl_seconds=30)
    cache.set("digest", [Chunk("old/app.py", "run", 1, 2, "def run():\n    pass")])

    restored = cache.get("digest", "new/app.py")

    assert restored == [Chunk("new/app.py", "run", 1, 2, "def run():\n    pass")]
    assert client.expiry == 30


def test_bm25_ranks_matching_chunk_and_formats_location():
    chunks = [
        Chunk("alpha.py", "alpha", 2, 3, "def alpha():\n    return 'needle'"),
        Chunk("beta.py", "beta", 8, 9, "def beta():\n    return 'other'"),
    ]

    results = BM25Retriever(chunks).search("needle", top_k=1)

    assert [result.chunk.path for result in results] == ["alpha.py"]
    rendered = format_results(results)
    assert "# alpha.py :: alpha (lines 2-3, score=" in rendered
    assert "return 'needle'" in rendered


def test_bm25_can_match_file_path_and_symbol_name():
    chunk = Chunk("src/payment_service.py", "calculate_total", 1, 2, "return 42")

    assert BM25Retriever([chunk]).search("payment_service calculate_total")[0].chunk == chunk


def test_bm25_handles_chinese_queries_and_empty_inputs():
    chunks = [
        Chunk("README.md", "intro", 1, 1, "项目支持代码检索"),
        Chunk("other.md", "other", 1, 1, "network client"),
    ]
    retriever = BM25Retriever(chunks)

    assert retriever.search("代码检索")[0].chunk.path == "README.md"
    assert retriever.search("") == []
    assert retriever.search("代码", top_k=0) == []
    assert BM25Retriever([]).search("anything") == []
    assert format_results([]) == "No matching code found."
