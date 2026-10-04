import copy
import json
import shlex
import subprocess
import sys
import textwrap

import pytest

from repo_agent.evaluation.repairs import (
    load_repair_cases,
    main,
    run_repair_case,
    summarize_repairs,
)
from repo_agent.models import MessageModel

CASES = load_repair_cases()
SOLUTIONS = {
    "pagination-boundary": {
        "pagination.py": (
            "def paginate(items, page, page_size):\n"
            "    if page < 1 or page_size < 1:\n"
            "        raise ValueError('positive values required')\n"
            "    start = (page - 1) * page_size\n"
            "    return items[start:start + page_size]\n"
        ),
    },
    "timeout-units": {
        "settings.py": (
            "def load_settings(env):\n"
            "    timeout = int(env.get('REPO_TIMEOUT_MS', '2500'))\n"
            "    if timeout <= 0:\n"
            "        raise ValueError('positive timeout required')\n"
            "    return {'timeout_ms': timeout}\n"
        ),
        "client.py": (
            "from settings import load_settings\n\n"
            "def request_options(env):\n"
            "    return {'timeout_seconds': load_settings(env)['timeout_ms'] / 1000}\n"
        ),
    },
}


class ScriptedRepairModel(MessageModel):
    def __init__(self, case, changes):
        super().__init__()
        commands = [f"cat {shlex.quote(path)}" for path in case["files"]]
        for path, content in changes.items():
            commands.append({"type": "edit", "mode": "replace" if path in case["files"] else "create",
                             "path": path, "old_text": case["files"].get(path, ""), "new_text": content})
        commands += [shlex.join([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"]),
                     "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
        self.commands = iter(commands)

    def query(self, messages):
        return self.format_message("Implemented the repair and regression tests; observed unittest passing.",
                                   [{"command": next(self.commands)}])


def changes(case):
    return {**SOLUTIONS[case["id"]], "tests/test_regression_behavior.py": (
        "import unittest\n\nclass Regression(unittest.TestCase):\n"
        "    def test_behavior(self):\n" + textwrap.indent(case["acceptance"], "        ")
    )}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_repair_evaluation_uses_real_workflow_and_never_approves(tmp_path, monkeypatch, case):
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: ScriptedRepairModel(case, changes(case)))
    result = run_repair_case(case, tmp_path / "run")
    assert result["passed"], result
    assert result["review_ready"]
    assert result["task_status"] == "awaiting_review"
    assert result["baseline"]["state"] == "failed"
    assert result["post_verification"]["state"] == "passed"
    assert result["source_unchanged"]
    assert not result["false_candidate"]
    assert (tmp_path / "run/task.json").is_file()
    assert (tmp_path / "run/trajectory.json").is_file()
    task = json.loads((tmp_path / "run/task.json").read_text())
    assert task["review"] is None
    assert task["worktree_merged"] is False
    assert task["candidate"]["commit"] == result["candidate_commit"]
    head = subprocess.run(["git", "-C", str(tmp_path / "run/source"), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    assert head == task["candidate"]["base_commit"]
    assert head != task["candidate"]["commit"]
    hooks = subprocess.run(["git", "-C", str(tmp_path / "run/source"), "config", "core.hooksPath"],
                           check=True, capture_output=True, text=True).stdout.strip()
    assert hooks == "/dev/null"
    assert (tmp_path / "run/candidate.patch").read_text() == task["candidate"]["diff"]


def test_review_ready_does_not_mean_independent_acceptance_passed(tmp_path, monkeypatch):
    case = CASES[0]
    partial = {
        "pagination.py": (
            "def paginate(items, page, page_size):\n"
            "    start = (page - 1) * page_size\n"
            "    return items[start:start + page_size]\n"
        ),
        "tests/test_regression_page.py": (
            "import unittest\nfrom pagination import paginate\nclass Regression(unittest.TestCase):\n"
            "    def test_page(self):\n        self.assertEqual(paginate([1], 1, 1), [1])\n"
        ),
    }
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: ScriptedRepairModel(case, partial))
    result = run_repair_case(case, tmp_path / "partial")
    assert result["review_ready"], result
    assert result["post_verification"]["state"] == "passed"
    assert result["public_tests"]["passed"]
    assert not result["acceptance"]["passed"]
    assert result["false_candidate"]
    assert not result["passed"]
    assert summarize_repairs([result])["false_candidate_rate"] == 1


def test_correct_candidate_without_regression_tests_does_not_pass(tmp_path, monkeypatch):
    case = CASES[0]
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: ScriptedRepairModel(case, SOLUTIONS[case["id"]]))
    result = run_repair_case(case, tmp_path / "no-tests")
    assert result["review_ready"]
    assert result["acceptance"]["passed"]
    assert not result["public_tests"]["passed"]
    assert result["false_candidate"]


def test_provider_failure_keeps_artifacts_and_remains_in_denominator(tmp_path, monkeypatch):
    def unavailable(config):
        raise RuntimeError("provider offline")

    monkeypatch.setattr("repo_agent.service.get_model", unavailable)
    result = run_repair_case(CASES[0], tmp_path / "offline")
    assert result["task_status"] == "error"
    assert "provider offline" in result["error"]
    assert result["steps"] == 0
    assert result["source_unchanged"]
    assert not result["false_candidate"]
    assert (tmp_path / "offline/task.json").is_file()
    summary = summarize_repairs([result])
    assert summary["runs"] == 1
    assert summary["passed"] == 0
    assert summary["source_mutations"] == 0


def test_source_mutation_is_a_failed_grade_even_for_a_correct_candidate(tmp_path, monkeypatch):
    from repo_agent.api import execute_task

    case = CASES[0]
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: ScriptedRepairModel(case, changes(case)))

    def modifying_execute(spec, **kwargs):
        result = execute_task(spec, **kwargs)
        (tmp_path / "mutation/source/README.md").write_text("Outside candidate changes\n")
        return result

    monkeypatch.setattr("repo_agent.api.execute_task", modifying_execute)
    result = run_repair_case(case, tmp_path / "mutation")
    assert result["review_ready"]
    assert result["acceptance"]["passed"]
    assert result["source_unchanged"] is False
    assert not result["passed"]
    assert result["false_candidate"]
    assert summarize_repairs([result])["source_mutations"] == 1


def test_invalid_baseline_does_not_start_a_model(tmp_path, monkeypatch):
    case = copy.deepcopy(CASES[0])
    case["files"]["tests/test_ci_failure.py"] = "# No failing CI test\n"
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: pytest.fail("Model must not start"))
    result = run_repair_case(case, tmp_path / "invalid")
    assert "failing CI baseline" in result["error"]
    assert not result["review_ready"]
    assert result["source_unchanged"]
    assert (tmp_path / "invalid/result.json").is_file()


def test_cli_repeats_and_never_overwrites_existing_reports(tmp_path):
    output = tmp_path / "results"
    assert main(["--case", "pagination-boundary", "--repeat", "2", "--max-steps", "2",
                 "--output", str(output)]) == 1
    report = json.loads((output / "summary.json").read_text())
    assert report["summary"]["runs"] == 2
    assert report["summary"]["review_ready"] == 0
    assert report["summary"]["passed"] == 0
    assert report["summary"]["source_mutations"] == 0
    assert [result["attempt"] for result in report["results"]] == [1, 2]
    assert len(report["suite_sha256"]) == 64
    assert report["results"][0]["configuration"]["provider"] == "mock"
    with pytest.raises(SystemExit) as exc:
        main(["--output", str(output)])
    assert exc.value.code == 2
    assert json.loads((output / "summary.json").read_text()) == report


@pytest.mark.parametrize("arguments", [
    ["--case", "stable-deduplicate"], ["--repeat", "0"], ["--max-steps", "101"],
    ["--deadline", "nan"], ["--timeout", "0"], ["--provider", "huggingface"],
])
def test_cli_rejects_invalid_inputs_before_creating_output(tmp_path, arguments):
    output = tmp_path / "unused"
    with pytest.raises(SystemExit) as exc:
        main([*arguments, "--output", str(output)])
    assert exc.value.code == 2
    assert not output.exists()


def test_cli_lists_only_repair_cases(tmp_path, capsys):
    assert main(["--list", "--output", str(tmp_path / "unused")]) == 0
    text = capsys.readouterr().out
    assert "pagination-boundary" in text
    assert "timeout-units" in text
    assert "stable-deduplicate" not in text
    assert not (tmp_path / "unused").exists()


def test_source_metrics_do_not_call_unknown_checks_mutations():
    results = [
        {"review_ready": ready, "passed": passed, "false_candidate": false, "source_unchanged": unchanged, "steps": 2}
        for ready, passed, false, unchanged in [
            (True, True, False, True), (True, False, True, False), (False, False, False, None),
        ]
    ]
    summary = summarize_repairs(results)
    assert summary["runs"] == 3
    assert summary["review_ready"] == 2
    assert summary["pass_rate"] == 1 / 3
    assert summary["false_candidate_rate"] == .5
    assert summary["source_mutations"] == 1
    assert summary["source_checks_missing"] == 1
