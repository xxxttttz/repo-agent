"""Source indexing, optional Redis caching, and lexical retrieval."""

from .cache import ChunkCache, RedisChunkCache
from .indexer import Chunk, IndexStats, build_index
from .retriever import BM25Retriever, SearchResult, format_results

__all__ = [
    "BM25Retriever",
    "Chunk",
    "ChunkCache",
    "IndexStats",
    "RedisChunkCache",
    "SearchResult",
    "build_index",
    "format_results",
]
