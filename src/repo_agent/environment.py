"""Compatibility exports; use :mod:`repo_agent.environments.local`."""

from .environments.local import DangerousCommandPolicy, ExecutionResult, ExecutionStatus, LocalEnvironment

Environment = LocalEnvironment

__all__ = ["DangerousCommandPolicy", "Environment", "ExecutionResult", "ExecutionStatus", "LocalEnvironment"]
