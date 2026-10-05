"""Grade the repair workflow on disposable Git fixtures, without approving it.

Public CI tests reproduce one symptom. Independent acceptance checks remain
outside the model context, so a review-ready candidate is not automatically a
passing grade. These small fixtures are not a production success-rate estimate.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from ..api import RepairRequest, TaskManager
from ..repairs import RepairProfile, run_checks
from .runner import acceptance_check, load_cases, summary_coverage

_CI_TESTS = {
    "pagination-boundary": (
        "import unittest\nfrom pagination import paginate\n\n"
        "class CIFailure(unittest.TestCase):\n"
        "    def test_first_page(self):\n"
        "        self.assertEqual(paginate([1, 2, 3], 1, 2), [1, 2])\n"
    ),
    "timeout-units": (
        "import unittest\nfrom client import request_options\n\n"
        "class CIFailure(unittest.TestCase):\n"
        "    def test_default_seconds(self):\n"
        "        self.assertEqual(request_options({})['timeout_seconds'], 2.5)\n"
    ),
}

_TIMEOUT_CONTRACT_TESTS = (
    "import unittest\nfrom settings import load_settings\nfrom client import request_options\n\n"
    "class PublicAPIContract(unittest.TestCase):\n"
    "    def test_settings_return_shape(self):\n"
    "        for env, expected in [({}, 2500), ({'REPO_TIMEOUT_MS': '8000'}, 8000)]:\n"
    "            with self.subTest(env=env):\n"
    "                self.assertEqual(load_settings(env), {'timeout_ms': expected})\n\n"
    "    def test_client_return_shape(self):\n"
    "        for env, expected in [({}, 2.5), ({'REPO_TIMEOUT_MS': '8000'}, 8.0)]:\n"
    "            with self.subTest(env=env):\n"
    "                self.assertEqual(request_options(env), {'timeout_seconds': expected})\n"
    "\n    def test_settings_reject_invalid_timeouts(self):\n"
    "        for raw in ['0', '-1', 'invalid', '1.5', '']:\n"
    "            with self.subTest(raw=raw):\n"
    "                with self.assertRaises(ValueError):\n"
    "                    load_settings({'REPO_TIMEOUT_MS': raw})\n"
    "\n    def test_client_reject_invalid_timeouts(self):\n"
    "        for raw in ['0', '-1', 'invalid', '1.5', '']:\n"
    "            with self.subTest(raw=raw):\n"
    "                with self.assertRaises(ValueError):\n"
    "                    request_options({'REPO_TIMEOUT_MS': raw})\n"
)

_REGRESSION_GATE = (
    "import sys\nimport unittest\nfrom pathlib import Path\n\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
    "suite = unittest.defaultTestLoader.discover('tests', pattern='test_regression_*.py')\n"
    "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
    "if not result.wasSuccessful():\n"
    "    raise SystemExit('New regression tests failed; fix them before submitting.')\n"
    "if result.testsRun <= len(result.skipped) + len(result.expectedFailures):\n"
    "    raise SystemExit('Add at least one runnable, non-skipped, non-expected-failure unittest test in tests/test_regression_*.py.')\n"
    "print('REGRESSION_TESTS_PASSED')\n"
)


def load_repair_cases() -> list[dict]:
    cases = []
    for original in load_cases():
        if original["id"] not in _CI_TESTS:
            continue
        case = copy.deepcopy(original)
        case["files"]["tests/test_ci_failure.py"] = _CI_TESTS[case["id"]]
        case["files"][".gitignore"] = "__pycache__/\n.pytest_cache/\n"
        case["files"]["ci/check_regressions.py"] = _REGRESSION_GATE
        case["regression_gate_version"] = "runnable-regression-v1"
        case["minimum_tests"] = 3  # Two original tests plus at least one new regression.
        if case["id"] == "timeout-units":
            case["files"]["tests/test_api_contract.py"] = _TIMEOUT_CONTRACT_TESTS
            case["public_contract_version"] = "timeout-contract-v2"
            case["task"] += (
                " Preserve the exact existing return dictionaries: load_settings returns only "
                "the timeout_ms key; request_options returns only the timeout_seconds key. "
                "Do not add extra keys or change the public return structure. "
                "The public API contract tests are protected and must pass."
            )
            case["minimum_tests"] = 7  # Six fixed tests plus at least one model-added regression.
        case["allowed_changes"] = [path for path in case["allowed_changes"] if not path.startswith("tests/")]
        case["allowed_changes"].append("tests/test_regression_*.py")
        case["protected_paths"] = [path for path in case["files"]
                                   if path.startswith("tests/")
                                   or path in {"README.md", ".gitignore", "ci/check_regressions.py"}]
        case["task"] += (
            " Original tests are protected. Add regression tests as new tests/test_regression_*.py files. "
            "The trusted regression gate requires at least one runnable, non-skipped, non-expected-failure "
            "unittest test in these new files; fixed tests, empty files and skipped-only or "
            "expected-failure-only tests do not satisfy it. "
            "Do not commit or merge yourself; the repair controller creates the candidate for human review."
        )
        cases.append(case)
    return cases


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _save(path: Path, data: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _profile(case: dict, *, provider: str, model: str | None, max_steps: int,
             timeout: float, deadline: float) -> RepairProfile:
    if provider != "mock" and (not isinstance(model, str) or not model.strip()):
        raise ValueError("Online repair evaluation requires an explicit model for reproducibility")
    command = shlex.join([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"])
    regression_command = shlex.join([sys.executable, "-B", "ci/check_regressions.py"])
    return RepairProfile(
        id=case["id"], workspace="source", title=f"CI fixture: {case['id']}",
        provider=provider, model=model, max_steps=max_steps, command_timeout=timeout,
        deadline_seconds=deadline, max_changed_files=5,
        reproduce_commands=(command,), verification_commands=(command, regression_command, "git diff --check"),
        allowed_paths=tuple(case["allowed_changes"]), protected_paths=tuple(case["protected_paths"]),
    )


def run_repair_case(case: dict, artifact_dir: Path, *, provider: str = "mock", model: str | None = None,
                    max_steps: int = 20, timeout: float = 30, deadline: float = 600) -> dict:
    profile = _profile(case, provider=provider, model=model, max_steps=max_steps, timeout=timeout, deadline=deadline)
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=False)
    source = artifact_dir / "source"
    source.mkdir()
    started = time.monotonic()
    record, manager = {}, None
    result = {
        "case_id": case["id"], "passed": False, "review_ready": False, "false_candidate": False,
        "public_contract_version": case.get("public_contract_version"),
        "regression_gate_version": case.get("regression_gate_version"),
        "task_status": "error", "agent_status": None, "steps": 0, "error": None,
        "configuration": profile.serialize(), "independent_baseline": None,
        "initial_public_checks": None, "acceptance": None, "public_tests": None,
        "source_unchanged": None, "changed_files": [], "candidate_commit": None,
        "summary_coverage": None,
    }
    task_id, base, source_branch = None, None, None
    try:
        for relative, content in case["files"].items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _git(source, "init", "-q")
        # Fixture runs must not execute hooks inherited from a user's Git config.
        _git(source, "config", "core.hooksPath", "/dev/null")
        _git(source, "add", "--all")
        _git(source, "-c", "user.name=Repair Evaluation", "-c", "user.email=eval@example.invalid",
             "commit", "-qm", "CI repair baseline")
        base = _git(source, "rev-parse", "HEAD")
        source_branch = _git(source, "symbolic-ref", "--short", "HEAD")
        result["base_commit"] = base
        result["independent_baseline"] = acceptance_check(case, source, timeout)
        initial = run_checks(source, profile.reproduce_commands, {}, timeout=timeout, cancelled=lambda: False)
        result["initial_public_checks"] = initial
        if result["independent_baseline"]["passed"] or initial["state"] != "failed":
            raise ValueError("Fixture must have both a failing CI baseline and failing independent acceptance")
        manager = TaskManager(artifact_dir, redis_url=None, workers=1, worktree_root=artifact_dir / "worktrees",
                              repair_profiles={profile.id: profile})
        accepted = manager.submit_repair(RepairRequest(
            profile=profile.id, task=case["task"],
            failure_log="\n".join(check["output"] for check in initial["checks"])[:40_000],
        ))
        task_id = accepted["id"]
        while True:
            record = manager.get(task_id)
            if record["status"] not in {"queued", "running"}:
                break
            time.sleep(.05)
        trajectory = record.get("result") or {}
        result.update(
            task_status=record["status"], agent_status=trajectory.get("status"),
            steps=len(trajectory.get("steps", [])), error=record.get("error"),
            review_ready=record["status"] == "awaiting_review",
            baseline=record.get("baseline"), post_verification=record.get("post_verification"),
            scope_review=record.get("scope_review"), handoff=trajectory.get("handoff"),
            summary_coverage=summary_coverage(case, trajectory.get("answer", ""), trajectory.get("status", "error")),
        )
        workspace = Path(record["execution_workspace"]) if record.get("execution_workspace") else source
        result["acceptance"] = acceptance_check(case, workspace, timeout)
        result["public_tests"] = acceptance_check({"acceptance": (
            "import unittest\n"
            "suite = unittest.defaultTestLoader.discover('tests')\n"
            "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
            "assert result.wasSuccessful(), 'public tests failed'\n"
            f"assert result.testsRun >= {case['minimum_tests']}, 'add regression tests'\n"
        )}, workspace, timeout)
        candidate = record.get("candidate") or {}
        result["changed_files"] = candidate.get("changed_paths", [])
        result["candidate_commit"] = candidate.get("commit")
        result["source_unchanged"] = (_git(source, "rev-parse", "HEAD") == base
                                      and _git(source, "symbolic-ref", "--short", "HEAD") == source_branch
                                      and not _git(source, "status", "--porcelain", "--untracked-files=all"))
        regression_added = any(path.startswith("tests/test_regression_") and path.endswith(".py")
                               for path in result["changed_files"])
        result["passed"] = (result["review_ready"] and result["acceptance"]["passed"]
                            and result["public_tests"]["passed"] and result["source_unchanged"] and regression_added)
        result["false_candidate"] = result["review_ready"] and not result["passed"]
    except Exception as error:  # noqa: BLE001 - keep failed attempts in the denominator.
        result["error"] = f"{type(error).__name__}: {error}"
        result["passed"] = False
        result["false_candidate"] = result["review_ready"]
    finally:
        if manager is not None:
            if task_id is not None:
                manager.cancel(task_id)  # No effect on terminal/awaiting-review tasks.
                record = manager.get(task_id) or record
            manager.close()
        if base is not None:
            try:
                result["source_unchanged"] = (_git(source, "rev-parse", "HEAD") == base
                                              and _git(source, "symbolic-ref", "--short", "HEAD") == source_branch
                                              and not _git(source, "status", "--porcelain", "--untracked-files=all"))
            except (OSError, subprocess.CalledProcessError) as error:
                result["source_unchanged"] = None
                result["source_check_error"] = f"{type(error).__name__}: {error}"
            if result["source_unchanged"] is not True:
                result["passed"] = False
            result["false_candidate"] = result["review_ready"] and not result["passed"]
        if record:
            _save(artifact_dir / "task.json", record)
            if record.get("result"):
                _save(artifact_dir / "trajectory.json", record["result"])
            if record.get("candidate"):
                (artifact_dir / "candidate.patch").write_text(record["candidate"]["diff"], encoding="utf-8")
        result["elapsed_seconds"] = round(time.monotonic() - started, 4)
        _save(artifact_dir / "result.json", result)
    return result


def summarize_repairs(results: list[dict]) -> dict:
    ready = sum(result["review_ready"] for result in results)
    passed = sum(result["passed"] for result in results)
    false = sum(result["false_candidate"] for result in results)
    return {
        "runs": len(results), "passed": passed, "pass_rate": passed / len(results) if results else 0,
        "review_ready": ready, "false_candidates": false,
        "false_candidate_rate": false / ready if ready else 0,
        "source_mutations": sum(result["source_unchanged"] is False for result in results),
        "source_checks_missing": sum(result["source_unchanged"] is None for result in results),
        "mean_steps": sum(result["steps"] for result in results) / len(results) if results else 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate CI repair candidates on built-in Git fixtures; never approve them.")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--provider", choices=("mock", "openrouter", "groq", "huggingface"), default="mock")
    parser.add_argument("--model")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--deadline", type=float, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    cases = load_repair_cases()
    if args.list:
        for case in cases:
            print(f"{case['id']}\tCI repair")
        return 0
    if not 1 <= args.repeat <= 100:
        parser.error("repeat must be between 1 and 100")
    try:
        _profile(cases[0], provider=args.provider, model=args.model, max_steps=args.max_steps,
                 timeout=args.timeout, deadline=args.deadline)
    except ValueError as error:
        parser.error(str(error))
    if args.case_ids:
        unknown = set(args.case_ids) - {case["id"] for case in cases}
        if unknown:
            parser.error(f"Unknown repair cases: {', '.join(sorted(unknown))}")
        cases = [case for case in cases if case["id"] in args.case_ids]
    output = (args.output or Path("eval-results") / f"repairs-{uuid4().hex}").resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error(f"Output directory already exists: {output}")
    results = []
    suite_hash = hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest()
    for case in cases:
        for attempt in range(1, args.repeat + 1):
            result = run_repair_case(case, output / f"{case['id']}-{attempt}", provider=args.provider,
                                     model=args.model, max_steps=args.max_steps, timeout=args.timeout,
                                     deadline=args.deadline)
            result["attempt"] = attempt
            _save(output / f"{case['id']}-{attempt}" / "result.json", result)
            results.append(result)
            _save(output / "summary.json", {
                "schema_version": 1, "suite_sha256": suite_hash,
                "scope": "Built-in Git fixture candidate evaluation; no approval, HTTP deployment or production success-rate claim.",
                "configuration": {"provider": args.provider, "model": args.model, "repeat": args.repeat,
                                  "max_steps": args.max_steps, "timeout": args.timeout, "deadline": args.deadline},
                "summary": summarize_repairs(results), "results": results,
            })
            print(f"{case['id']} [{attempt}]: {'PASS' if result['passed'] else 'FAIL'} "
                  f"(task={result['task_status']}, steps={result['steps']})", flush=True)
    print(f"Results: {output / 'summary.json'}")
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
