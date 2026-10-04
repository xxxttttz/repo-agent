"""Evaluate generated code independently of the agent's completion status.

These tiny fixtures are regression tasks, not an adversarial sandbox or an
estimate of performance on arbitrary production repositories.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shlex
import stat
import sys
import time
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from ..agents import get_agent
from ..config import load_config
from ..environments.local import ExecutionStatus, LocalEnvironment
from ..models import ModelBackend, get_model

_IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}


def load_cases() -> list[dict]:
    return json.loads(files("repo_agent.evaluation").joinpath("cases.json").read_text(encoding="utf-8"))


def snapshot(workspace: Path) -> dict[str, str]:
    """Hash files, including new/deleted paths; never follow directory symlinks."""
    result = {}
    for root, dirs, names in os.walk(workspace, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in _IGNORED_DIRS)
        for name in sorted(names + [name for name in dirs if (Path(root) / name).is_symlink()]):
            path = Path(root) / name
            relative = path.relative_to(workspace).as_posix()
            if path.is_symlink():
                result[relative] = "symlink:" + os.readlink(path)
                continue
            mode = path.stat().st_mode
            if not stat.S_ISREG(mode):
                result[relative] = f"special:{mode}"
                continue
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            result[relative] = digest.hexdigest()
    return result


def acceptance_check(case: dict, workspace: Path, timeout: float) -> dict:
    # The checker source comes from the installed suite, never from generated
    # tests. -I/-B avoid PYTHONPATH, user site configuration and bytecode writes.
    marker = f"ACCEPTED_{uuid4().hex}"
    source = "import sys\nsys.path.insert(0, sys.argv[1])\n" + case["acceptance"]
    source += f"\nprint({marker!r})\n"
    command = shlex.join([sys.executable, "-I", "-B", "-c", source, str(workspace)])
    execution = LocalEnvironment(str(workspace), timeout=timeout, inherit_env=False).execute(command)
    return {
        "passed": execution.status is ExecutionStatus.SUCCESS and marker in execution.output.splitlines(),
        "status": execution.status.value,
        "returncode": execution.returncode,
        "output": execution.output.replace(marker, "ACCEPTANCE_PASSED"),
        "error": execution.error,
        "truncated": execution.truncated,
    }


def summary_coverage(case: dict, answer: str, status: str) -> dict:
    """A diagnostic for literal subject coverage, never a semantic judge."""
    subjects = case.get("summary_subjects", [])
    evaluated = status == "completed" and bool(subjects)
    missing = [subject for subject in subjects if subject not in answer]
    return {"evaluated": evaluated, "passed": evaluated and not missing,
            "required_subjects": list(subjects), "missing_subjects": missing,
            "scope": "Literal subject coverage only; does not verify claims or summary quality."}


def run_case(
    case: dict,
    artifact_dir: Path,
    model_factory: Callable[[], ModelBackend],
    *,
    max_steps: int = 20,
    timeout: float = 30,
    protected_paths: tuple[str, ...] = (),
    verification_commands: tuple[str, ...] = (),
) -> dict:
    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=False)
    workspace = artifact_dir / "workspace"
    workspace.mkdir()
    for relative, content in case["files"].items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    initial = snapshot(workspace)
    started = time.monotonic()
    baseline = acceptance_check(case, workspace, timeout)
    agent_started = time.monotonic()
    status, answer, steps, error, model_info = "error", "", 0, None, None
    agent = None
    try:
        if baseline["passed"] != case["baseline_passes"]:
            raise ValueError("Fixture baseline does not match its declared expectation")
        model = model_factory()
        model_info = {"class": type(model).__name__, "model_name": getattr(model, "model_name", None)}
        config = load_config()["agent"]
        config["max_steps"] = max_steps
        config["protected_paths"] = list(protected_paths)
        config["verification_commands"] = list(verification_commands)
        agent = get_agent(model, LocalEnvironment(str(workspace), timeout=timeout, inherit_env=False), config)
        result = agent.run(case["task"])
        status, answer, steps = result.status.value, result.answer, result.step_count
        if status == "error":
            error = answer
    except Exception as exc:  # noqa: BLE001 - keep the rest of a benchmark run evaluable.
        error = f"{type(exc).__name__}: {exc}"
    agent_elapsed = time.monotonic() - agent_started
    if agent is not None:
        agent.save(artifact_dir / "trajectory.json")
    # Evaluate even an unfinished task, separating working code from completion.
    acceptance = acceptance_check(case, workspace, timeout)
    public_tests = None
    if case["category"] != "read-only":
        # Each fixture starts with one public test. Require an expanded,
        # passing suite, in addition to our independent behavioral assertions.
        public_tests = acceptance_check({"acceptance": (
            "import unittest\n"
            "suite = unittest.defaultTestLoader.discover('tests')\n"
            "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
            "assert result.wasSuccessful(), 'public tests failed'\n"
            "assert result.testsRun >= 2, 'add regression tests'\n"
        )}, workspace, timeout)
    final = snapshot(workspace)
    changed = sorted(path for path in initial.keys() | final.keys() if initial.get(path) != final.get(path))
    unrelated = [path for path in changed if not any(fnmatch.fnmatchcase(path, pattern)
                                                   for pattern in case["allowed_changes"])]
    missing_facts = [fact for fact in case["answer_contains"] if fact not in answer]
    passed = (status == "completed" and acceptance["passed"] and not unrelated and not missing_facts
              and (public_tests is None or public_tests["passed"]))
    report = {
        "case_id": case["id"], "category": case["category"],
        "agent_status": status, "passed": passed,
        "false_completion": status == "completed" and not passed,
        "steps": steps, "elapsed_seconds": round(time.monotonic() - started, 4),
        "agent_elapsed_seconds": round(agent_elapsed, 4), "model": model_info,
        "changed_files": changed, "unrelated_changes": unrelated,
        "missing_answer_facts": missing_facts, "baseline": baseline, "acceptance": acceptance,
        "public_tests": public_tests,
        "error": error, "answer": answer,
        "handoff": agent.serialize().get("handoff", {}) if agent is not None else {},
        "summary_coverage": summary_coverage(case, answer, status),
    }
    (artifact_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def summarize(results: list[dict]) -> dict:
    total = len(results)
    completed = sum(result["agent_status"] == "completed" for result in results)
    false_completions = sum(result["false_completion"] for result in results)
    summaries = [result["summary_coverage"] for result in results
                 if result.get("summary_coverage", {}).get("evaluated")]
    return {
        "runs": total,
        "passed": sum(result["passed"] for result in results),
        "pass_rate": sum(result["passed"] for result in results) / total if total else 0,
        "completed": completed,
        "false_completions": false_completions,
        "false_completion_rate": false_completions / completed if completed else 0,
        "mean_steps": sum(result["steps"] for result in results) / total if total else 0,
        "mean_elapsed_seconds": sum(result["elapsed_seconds"] for result in results) / total if total else 0,
        "unrelated_changes": sum(len(result["unrelated_changes"]) for result in results),
        "summaries_evaluated": len(summaries),
        "summaries_with_required_subjects": sum(summary["passed"] for summary in summaries),
        "summary_subject_coverage_rate": (sum(summary["passed"] for summary in summaries) / len(summaries)
                                          if summaries else 0),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated fixture tasks and independently grade their results.")
    parser.add_argument("--list", action="store_true", help="List built-in cases without running a model.")
    parser.add_argument("--case", action="append", dest="case_ids", help="Case id; repeat to select multiple cases.")
    parser.add_argument("--provider", choices=("mock", "openrouter", "groq", "huggingface"), default="mock")
    parser.add_argument("--model")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=30, help="Timeout per shell/checker command in seconds.")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--protect", action="append", default=[], metavar="PATH",
                        help="Protect this file in every case; recorded as part of evaluation configuration.")
    parser.add_argument("--verify", action="append", default=[], metavar="COMMAND",
                        help="Require this shell check on each submission; repeat for multiple checks.")
    parser.add_argument("--output", type=Path, help="New artifact directory; existing directories are never overwritten.")
    args = parser.parse_args(argv)
    cases = load_cases()
    if args.list:
        for case in cases:
            print(f"{case['id']}\t{case['category']}")
        return 0
    if args.repeat < 1 or args.max_steps < 1 or args.timeout <= 0:
        parser.error("repeat, max-steps and timeout must be positive")
    if any(not command.strip() for command in args.verify):
        parser.error("verification commands must be non-empty")
    if args.case_ids:
        unknown = set(args.case_ids) - {case["id"] for case in cases}
        if unknown:
            parser.error(f"Unknown case ids: {', '.join(sorted(unknown))}")
        cases = [case for case in cases if case["id"] in args.case_ids]
    output = (args.output or Path("eval-results") / uuid4().hex).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error(f"Output directory already exists: {output}")
    model_config = {"model_class": args.provider, "model_name": args.model}
    results = []
    suite_hash = hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest()
    for case in cases:
        for attempt in range(1, args.repeat + 1):
            result = run_case(case, output / f"{case['id']}-{attempt}", lambda: get_model(model_config),
                              max_steps=args.max_steps, timeout=args.timeout, protected_paths=tuple(args.protect),
                              verification_commands=tuple(args.verify))
            result["attempt"] = attempt
            results.append(result)
            summary = {"schema_version": 1, "suite_sha256": suite_hash,
                       "configuration": {"provider": args.provider, "model": args.model,
                                         "max_steps": args.max_steps, "timeout": args.timeout, "repeat": args.repeat,
                                         "protected_paths": args.protect,
                                         "verification_commands": args.verify},
                       "summary": summarize(results), "results": results}
            temporary = output / ".summary.tmp"
            temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(output / "summary.json")
            print(f"{case['id']} [{attempt}]: {'PASS' if result['passed'] else 'FAIL'} "
                  f"(agent={result['agent_status']}, steps={result['steps']})", flush=True)
    print(f"Results: {output / 'summary.json'}")
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
