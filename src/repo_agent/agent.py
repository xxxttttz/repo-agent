"""Compatibility export; use :mod:`repo_agent.agents.default`."""

from .agents.default import DefaultAgent

Agent = DefaultAgent

__all__ = ["Agent", "DefaultAgent"]
