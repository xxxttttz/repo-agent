# Changelog

All notable changes to Repo Agent are documented in this file.

The format follows Keep a Changelog, and the project uses Semantic Versioning.

## [Unreleased]

### Added

- Upfront and resume-aware standalone-read plans for the built-in file-evidence
  policy, complete resolved-file blockers on rejection, and progress/resubmission
  guidance after evidence repair. Compound reads still do not count; guidance
  never auto-submits, invokes custom policies early or bypasses required checks.

- Read-only, commit/hash-bound candidate patch and minimal review-evidence
  downloads, with stored-byte checksum validation, safe attachment filenames,
  authentication and no-store/nosniff headers. Console downloads use Bearer
  headers rather than tokenized URLs, with stale-view protection and optional
  Web Crypto patch checksum verification. Exports never approve or push changes.

- Authenticated cursor-paginated task summaries and console history, with
  status/kind/workspace filters, workspace-root boundaries, a bounded Redis
  history index, expiry pruning, batch metadata reads, and explicit degraded
  local-cache reporting. Lists never include logs, trajectories or full patches.

- `repo-agent-repair-eval` grades the production repair controller on fresh
  Git fixtures, with reproducible public CI failures, protected original tests,
  independent behavioral acceptance, source-mutation detection, repetitions,
  retained task/trajectory/patch artifacts, and no automatic approvals.

- Internal Python CI repair console and profile-based `/repairs` API, with
  server-owned repository/check/scope/budget policy, failure reproduction before
  model execution, independent final verification, and retained candidate diffs.
- Exact commit/diff-hash-bound approval and rejection APIs. Approval reruns
  checks and validates clean candidate/source state before fast-forwarding;
  rejected and stale candidates never auto-merge and remain available for review.
- Optional shared Bearer authentication, loopback listener/Compose port defaults,
  and a per-process pending-task limit. Repair mode disables general task creation
  by default so callers cannot bypass profile policy.

- Runner-generated handoff receipts on every terminal status, kept separate
  from model prose in CLI, trajectory, API and evaluation results. Receipts
  distinguish absent/unrun checks and accepted verification, with successful
  structured edit action history and explicit scope limits.
- Diagnostic summary subject coverage in evaluations, independent of code
  grading; final-summary prompts also apply after rejected submissions.

- Budget reminders before the final three/one model actions, including built-in
  file-evidence blockers and resume-aware remaining-turn accounting; no forced
  completion or automatic budget extension.
- Evaluation CLI `--verify` for caller-selected submission checks, with explicit
  report configuration and unchanged independent grading.

- Structured exact text replacement and exclusive file creation actions, with
  Python syntax validation, workspace path checks, atomic writes, diff feedback,
  provider protocol/history support, and trajectory resume compatibility.
- Docker edit dispatch executes the stdlib editor in the configured container
  instead of modifying files through the host executor.

- Caller-selected protected file invariants via CLI `--protect`, YAML, HTTP,
  and evaluation runs, checked before/after verification and preserved on resume.

- `repo-agent-eval` with four packaged coding/investigation fixtures,
  independent acceptance checks, repeated runs, and retained JSON/trajectory
  artifacts reporting success, false completion, scope changes, steps, and time.

- Caller-configured completion checks through CLI `--verify`, YAML, and HTTP;
  each submission reruns checks and records diagnostics in the trajectory.
- CI lint checks alongside the existing test and build jobs.

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

### Changed

- Git HTTP tasks now default to manual review instead of automatic merge.
  Explicit `delivery_mode: "auto_merge"` retains the legacy behavior; repair
  requests always require review. Reviewed worktrees are retained after decisions.

### Fixed

- Provider request retries now include direct connection interruptions such as
  `RemoteDisconnected`, reset connections and broken pipes, bounded by the
  existing retry limit; exhausted retries retain their original cause.

- The deduplication evaluation task explicitly requires a list return value
  and runnable unittest tests, matching its acceptance criteria.

- Provider conversation history now includes the exact prior shell action
  in its original JSON format instead of discarding it and sending only prose.

- Repeated source reads now receive a recovery hint after three matching
  outputs within six steps; investigation prompts ask for the full implementation
  and its error paths early instead of repeatedly expanding documentation searches.

- Tasks without named files can no longer complete without a successful command.
- Compound shell commands and filename substrings no longer count as file reads.
- Unfinished, cancelled, and failed HTTP tasks retain their Git worktrees and
  branches instead of losing uncommitted changes during cleanup.

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
