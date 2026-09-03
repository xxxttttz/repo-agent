"""Dependency-free source indexing and lexical retrieval."""

from .indexer import Chunk, build_index
from .retriever import BM25Retriever, SearchResult, format_results

__all__ = ["BM25Retriever", "Chunk", "SearchResult", "build_index", "format_results"]
