# Coding task evaluation

This suite runs Repo Agent on small, reproducible Python repositories and
checks the resulting code independently of the agent's completion policy.
It currently contains four handcrafted regression tasks, not a representative
benchmark of production coding performance.

The first real-model iteration and its limitations are recorded in
[第一轮真实模型评测记录](live-evaluation.md).

| Case | Capability | Independent acceptance |
| --- | --- | --- |
| `pagination-boundary` | Single-file bug repair | Page boundaries, empty inputs, invalid arguments |
| `timeout-units` | Cross-file change | Millisecond/second conversion and input validation |
| `stable-deduplicate` | Add a feature | Stable ordering, generators, identity, key-call count |
| `explain-timeout` | Read-only investigation | Answer facts and unchanged files |

## Running

Install the project to get the `repo-agent-eval` entry point. From a source
checkout, the equivalent is `python -m repo_agent.evaluation.runner` with
`src` on `PYTHONPATH` (or an editable installation).

```bash
repo-agent-eval --list
repo-agent-eval --provider mock --max-steps 5 --output /tmp/repo-agent-mock-eval
repo-agent-eval --provider huggingface --model YOUR_MODEL \
  --max-steps 20 --repeat 3 --output /tmp/repo-agent-model-eval
repo-agent-eval --case pagination-boundary --case timeout-units \
  --provider openrouter --model YOUR_MODEL --output /tmp/repo-agent-selected-eval
repo-agent-eval --case pagination-boundary --provider huggingface \
  --protect README.md --max-steps 20 --output /tmp/repo-agent-protected-eval
repo-agent-eval --case stable-deduplicate --provider huggingface \
  --protect README.md --verify 'python3 -B -m unittest discover -s tests' \
  --max-steps 20 --output /tmp/repo-agent-verified-eval
```

Use a **new output directory** each time. Existing directories are rejected;
results are never overwritten. The default output is `eval-results/<unique-id>`.
Each case and repetition gets fresh files and a fresh model instance. The
runner does not edit the repository from which you launch it.

Repeat `--protect PATH` to enable the agent's protected-file completion guard
in every selected case. The explicit list is saved in the report configuration;
it does not change independent grading. Compare runs with the same protection
configuration when measuring model variability.

Repeat `--verify COMMAND` to supply trusted caller-selected checks. Every
eligible submission runs them again in the same environment, so an earlier
passing test run does not bypass verification after subsequent edits. Check
failures are fed back to the model and recorded in `trajectory.json`; commands
are saved in the report configuration. Use read-only checks, not formatters or
other commands that change the code being checked. The independent suite's
acceptance source and results remain outside the model conversation. Public
tests can miss bugs or be weakened, so passing these checks alone is not a
passing evaluation grade. Compare runs with identical verification settings.

For budgets of at least four model actions, the agent sends reminders before
the actions with three and one turns remaining. These include any current
built-in file-evidence blocker and explain that configured verification runs
outside the model-action budget. Resumed runs count the newly allocated turns,
not the total trajectory length. Reminders neither increase the budget nor
force submission; exhausting it still returns `max_steps`. Without explicit
checks, testing after the last edit is a prompt instruction, not a guaranteed
completion guard.

Mock requires no API key and makes no network requests. It does not solve
these tasks, so a failing grade and exit code 1 are expected. This validates
the reporting path, not model quality. Real providers use their existing
environment-based credentials and may incur costs. This implementation uses
the default agent prompt and local executor; it does not test HTTP queues,
retrieval injection, session memory, or Docker execution.

`--timeout` limits each shell or acceptance command, not the entire run or
model request. `--max-steps` limits model turns. Run untrusted models/code in
an isolated environment: a fresh workspace and an independent checker are
not operating-system isolation. The checker lives outside the generated
workspace, but this is not a defense against a malicious process with host
filesystem access.

## What counts as success

A run passes only when all of the following hold:

1. The agent returned `completed`.
2. The suite's independent behavioral assertions passed.
3. All final changed, added, and deleted files match the case's allowed paths.
4. Required answer facts are present for read-only tasks.
5. Coding tasks have a passing public unittest suite with at least two tests
   (fixtures start with one, and requests explicitly ask for regression tests).

The behavioral checker is supplied by the suite, not loaded from generated
tests. Removing or replacing the visible tests cannot make broken production
code pass these assertions. Test count is only a minimal check that tests were
added, not a measurement of their quality. Answer grading uses literal facts,
not a semantic judge. Fingerprints compare final file contents and symlink
targets, excluding `.git`, `__pycache__`, `.pytest_cache`, and `.ruff_cache`;
they do not detect temporary edits restored before completion or chmod-only
changes. These limits keep the initial suite small and its scores interpretable.

Acceptance runs after the agent stops, including on `max_steps` and `error`,
and is not fed back to the agent. Therefore `acceptance.passed=true` with
`passed=false` can mean working code that the agent did not finish submitting,
missing regression tests, unrelated edits, or missing answer facts. Baseline
acceptance is recorded before running the agent: the three coding fixtures
must fail and the unchanged read-only fixture must pass.

## Artifacts and metrics

Each case directory retains `workspace/`, `trajectory.json` when an agent was
created, and `result.json`. Setup/model errors are recorded per case so other
cases can continue. Root `summary.json` is atomically updated after each run,
with configuration, a hash of the selected task definitions, individual
results, and aggregate metrics:

- `pass_rate`: successful runs / all runs.
- `false_completion_rate`: completed-but-failed runs / completed runs; 0 when
  there were no completions. Always inspect the accompanying counts.
- `mean_steps`: model turns, including submission attempts.
- `mean_elapsed_seconds`: mean case runtime including acceptance checks.
- `unrelated_changes`: number of final paths outside the allowed change set.
- `summaries_evaluated`, `summaries_with_required_subjects`, and
  `summary_subject_coverage_rate`: literal fixture-subject coverage among
  completed answers, a separate diagnostic that does not change code grading.

Per-case results also record `agent_elapsed_seconds`, independent/public test
output, changed paths, missing answer facts, and the selected model identity.
They also retain the runner's `handoff` receipts and `summary_coverage`, with
the required/missing identifiers. Keyword coverage does not prove a correct or
useful summary. See [delivery and verification receipts](handoff.md).
Token usage and cost are not reported because current model adapters do not
expose them. Compare runs with the same suite hash, limits, model settings,
and runtime environment. Repetitions remain separate in the report so you
can inspect variability instead of keeping only the best attempt.

Exit code is 0 only if every run passes, 1 if any run fails, and 2 for invalid
CLI arguments. Workspaces are intentionally retained for inspection; clean
up only the output directories you no longer need.

## Extending and validating the suite

Add a case to `src/repo_agent/evaluation/cases.json` with fixture files, task,
allowed change paths, independent assertions, answer facts, and expected
baseline. These packaged definitions are trusted executable test material,
not input received from an untrusted API caller.

Update `tests/test_evaluation.py` with a reference solution. Tests exercise
baseline failures, correct solutions, false completion, tampered visible
tests, missing regression tests, unrelated edits, provider errors, checker
timeouts, and report preservation. Reference solutions exist only in tests;
the evaluation CLI never offers a solver that knows the answers.

```bash
python -m pytest -q tests/test_evaluation.py
```
