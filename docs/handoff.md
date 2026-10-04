# Delivery and verification receipts

The model's final answer and the runner's evidence are different outputs.
The prompt requests a self-contained summary in the user's language: changes
or findings, affected files/symbols, commands and results actually observed,
and remaining limitations or unverified behavior. A rejected submission must
not turn the next final answer into just a note about resubmitting. Required
checks run **after** the proposed answer, so the model must not invent their
results in that answer. These instructions do not prove summary accuracy.

Every `DefaultAgent` terminal result includes a `handoff` dictionary. It is
also saved in trajectories, returned by the HTTP service's result object, and
retained in evaluation reports. CLI output prints runner evidence separately
from the model's answer. No additional model turn is spent generating receipts;
`answer` and model messages are preserved, not silently rewritten.

## Fields

- `status` and `submission_accepted`: the actual terminal status. Only
  `completed` has an accepted submission. Cancellation, provider failure and
  exhausted budgets remain unfinished even if model prose claims otherwise.
- `successful_edit_actions`: step number, path and mode for successful
  structured edit actions, including pre-resume history. Failed edits and
  arbitrary shell writes are not listed. This is **not a final workspace diff**:
  a later action can undo a successful edit or change a file by other means.
- `verification`: caller-configured commands, latest attempted submission's
  step number, check receipts and the state described below. Each receipt has
  command, status, return code, error and truncation flag; full output remains
  in the trajectory's `verifications` field. Earlier submissions' check
  successes cannot appear as current-submission success.
- `protected_files`: configured paths and whether they were checked on the
  accepted submission. This flag is false if there is no accepted submission
  or no configured protection; it does not describe all workspace files.
- `limitations`: explicit scope notices. Neither these checks nor the receipt
  prove every requirement, the quality of generated tests or the accuracy of
  model claims. Caller checks should be trusted, read-only commands.

| Verification state | Meaning |
| --- | --- |
| `not_configured` | No caller-required checks; not equivalent to tested |
| `not_run` | Checks configured, but none ran for the latest submission (or none was attempted) |
| `not_accepted` | Some checks ran, but the task did not finish with an accepted submission |
| `passed` | All current configured checks passed on the accepted submission |
| `incomplete` | Accepted result lacks matching successful receipts for all configured checks; not equivalent to passed |

For example, missing file evidence rejects submission before verification,
so receipts say `not_run`. A failing test or cancellation during checking has
`not_accepted`, with the executed checks' actual statuses. A new rejected
submission never reuses receipts from a previous attempted submission. Resume
rebuilds receipts from steps and verification records; it does not trust the
saved handoff dictionary.

## Evaluation diagnostic

`summary_coverage` checks whether a completed answer contains the fixture's
literal `summary_subjects` (for example, `unique_by` and `records.py`). Reports
record required and missing subjects and aggregate counts/rate among evaluated
completed answers. Unfinished runs are not counted as evaluated summaries.

This is a small diagnostic, **not a semantic quality judge**. A false or vague
answer can contain all identifiers. It does not verify test claims, detect
hallucinated changes, or enforce limitations wording, and it does not change
independent code grading. Review the actual answer, receipts and workspace
together. Metadata changes alter the suite hash, so old and new reports are
not automatically comparable.

Provider connection interruptions are retried only up to the configured
`max_retries` limit, with existing backoff. Retrying a model request does not
replay workspace commands; it also does not guarantee provider availability
or prevent duplicate remote billing after a lost response. Exhaustion stays
an `error` result with unaccepted submission and truthful check receipts.
