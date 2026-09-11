# Changelog

All notable changes to Repo Agent are documented in this file.

The format follows Keep a Changelog, and the project uses Semantic Versioning.

## [Unreleased]

### Added

- Dependency-free source chunking and BM25 retrieval with English identifier
  and Chinese character matching.
- Local `repo-agent search` command for querying a workspace without a model or
  API key.
- Redis content-hash caching for per-file source chunks with graceful fallback.
- Asynchronous FastAPI task submission/status endpoints with indexed context.
- Docker and Compose deployment for the API and persistent Redis service.
- Workspace-scoped Redis conversation memory with session APIs, bounded history,
  and sliding TTL.
- Redis-backed task snapshots with a configurable retention period and local
  fallback when Redis is unavailable.
- Cooperative task cancellation and server-sent task status events.
- Redis Streams task delivery with consumer acknowledgements, ownership
  heartbeats, and automatic reclaiming of interrupted tasks.
- Renewable Redis workspace leases with ownership-safe release, lease-loss
  detection, and an in-process fallback.
- A one-shot Docker execution environment with restrictive network, filesystem,
  capability, CPU, memory, PID, and environment defaults.
- Environment-variable isolation for commands launched by the HTTP service.

## [0.1.0] - 2026-08-31

### Added

- Linear shell-action coding agent with YAML/Jinja configuration.
- Local execution environment with timeout, output limits, and basic destructive-command guards.
- Evidence-aware completion with an explicit submission marker.
- OpenRouter, Groq, Hugging Face Inference Providers, and Mock model adapters.
- Strict JSON action parsing and transient transport retries.
- Structured, redacted, atomically saved trajectories.
- Resume support for current and legacy unfinished trajectories.
- Portable launcher with shared-environment and system-Python discovery.
- Automated tests covering agent, environment, providers, configuration, CLI, launcher, and resume behavior.
