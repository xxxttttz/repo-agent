"""Chunk workspace source files into retrievable units.

Kept dependency-free (stdlib only) to match the project's minimal
dependency policy. Python files are split by function/class using
``ast``; everything else falls back to paragraph-sized line chunks.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
_TEXT_EXTENSIONS = {".py", ".md", ".txt", ".rst", ".yaml", ".yml", ".toml", ".json"}
_FALLBACK_CHUNK_LINES = 60


@dataclass(frozen=True, slots=True)
class Chunk:
    path: str
    name: str
    start_line: int
    end_line: int
    text: str


def build_index(cwd: str) -> list[Chunk]:
    """Walk ``cwd`` and return chunks for every indexable text file."""
    chunks: list[Chunk] = []
    for root, dirs, files in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for filename in sorted(files):
            ext = os.path.splitext(filename)[1]
            if ext not in _TEXT_EXTENSIONS:
                continue
            abs_path = os.path.join(root, filename)
            if os.path.islink(abs_path):
                continue
            rel_path = os.path.relpath(abs_path, cwd)
            try:
                with open(abs_path, "r", encoding="utf-8") as handle:
                    source = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
            if ext == ".py":
                chunks.extend(_chunk_python(rel_path, source))
            else:
                chunks.extend(_chunk_fallback(rel_path, source))
    return chunks


def _chunk_python(rel_path: str, source: str) -> list[Chunk]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _chunk_fallback(rel_path, source)

    lines = source.splitlines()
    chunks: list[Chunk] = []
    top_level_defs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    if not top_level_defs:
        return _chunk_fallback(rel_path, source)

    for node in top_level_defs:
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        text = "\n".join(lines[start - 1:end])
        if text.strip():
            chunks.append(Chunk(rel_path, node.name, start, end, text))
    return chunks


def _chunk_fallback(rel_path: str, source: str) -> list[Chunk]:
    lines = source.splitlines()
    chunks: list[Chunk] = []
    for start in range(0, len(lines), _FALLBACK_CHUNK_LINES):
        end = min(start + _FALLBACK_CHUNK_LINES, len(lines))
        text = "\n".join(lines[start:end])
        if text.strip():
            chunks.append(Chunk(rel_path, f"lines {start + 1}-{end}", start + 1, end, text))
    return chunks
