# Changelog

All notable changes to Repo Agent are documented in this file.

The format follows Keep a Changelog, and the project uses Semantic Versioning.

## [Unreleased]

### Added

- Dependency-free source chunking and BM25 retrieval with English identifier
  and Chinese character matching.
- Local `repo-agent search` command for querying a workspace without a model or
  API key.

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
