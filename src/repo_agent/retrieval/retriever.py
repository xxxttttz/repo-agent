"""A minimal, dependency-free BM25 retriever over indexed chunks.

Implemented from scratch (no rank_bm25 / embedding libraries) to keep
the project's small dependency footprint. Good enough as a baseline;
swap ``BM25Retriever`` for an embedding-based implementation later if
recall on paraphrased queries turns out to matter.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from .indexer import Chunk

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[\u3400-\u4dbf\u4e00-\u9fff]")
_K1 = 1.5
_B = 0.75


@dataclass(frozen=True, slots=True)
class SearchResult:
    chunk: Chunk
    score: float


def _tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in _TOKEN_RE.findall(text)]


class BM25Retriever:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self._doc_tokens = [
            _tokenize(f"{chunk.path} {chunk.name} {chunk.text}") for chunk in chunks
        ]
        self._doc_lengths = [len(tokens) for tokens in self._doc_tokens]
        self._avg_doc_length = (sum(self._doc_lengths) / len(self._doc_lengths)) if chunks else 0.0
        self._term_freqs = [Counter(tokens) for tokens in self._doc_tokens]
        self._doc_freq = self._build_doc_freq()
        self._idf = self._build_idf(len(chunks))

    def _build_doc_freq(self) -> Counter:
        doc_freq: Counter = Counter()
        for tokens in self._doc_tokens:
            doc_freq.update(set(tokens))
        return doc_freq

    def _build_idf(self, n_docs: int) -> dict[str, float]:
        idf: dict[str, float] = {}
        for term, freq in self._doc_freq.items():
            idf[term] = math.log(1 + (n_docs - freq + 0.5) / (freq + 0.5))
        return idf

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        if top_k <= 0:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens or not self.chunks:
            return []

        scores = [0.0] * len(self.chunks)
        for doc_idx, term_freq in enumerate(self._term_freqs):
            doc_len = self._doc_lengths[doc_idx]
            score = 0.0
            for term in query_tokens:
                freq = term_freq.get(term)
                if not freq:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = freq + _K1 * (1 - _B + _B * doc_len / max(self._avg_doc_length, 1e-9))
                score += idf * (freq * (_K1 + 1)) / denom
            scores[doc_idx] = score

        ranked = sorted(range(len(self.chunks)), key=lambda i: scores[i], reverse=True)
        results = [SearchResult(self.chunks[i], scores[i]) for i in ranked[:top_k] if scores[i] > 0]
        return results


def format_results(results: list[SearchResult]) -> str:
    """Render results as an observation-friendly text block."""
    if not results:
        return "No matching code found."
    blocks = []
    for result in results:
        chunk = result.chunk
        header = f"# {chunk.path} :: {chunk.name} (lines {chunk.start_line}-{chunk.end_line}, score={result.score:.2f})"
        blocks.append(f"{header}\n{chunk.text}")
    return "\n\n".join(blocks)
