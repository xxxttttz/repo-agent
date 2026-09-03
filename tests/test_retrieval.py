from repo_agent.retrieval import BM25Retriever, Chunk, build_index, format_results


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


def test_invalid_python_falls_back_to_line_chunks(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    chunks = build_index(str(tmp_path))

    assert len(chunks) == 1
    assert chunks[0].name == "lines 1-2"


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
