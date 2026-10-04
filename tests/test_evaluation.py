import json
import shlex
import sys
import textwrap

import pytest

from repo_agent.evaluation.runner import (
    acceptance_check,
    load_cases,
    main,
    run_case,
    summarize,
    summary_coverage,
)
from repo_agent.models import MessageModel

CASES = load_cases()
SOLUTIONS = {
    "pagination-boundary": {
        "pagination.py": "def paginate(items, page, page_size):\n    if page < 1 or page_size < 1:\n        raise ValueError('positive values required')\n    start = (page - 1) * page_size\n    return items[start:start + page_size]\n",
    },
    "timeout-units": {
        "settings.py": "def load_settings(env):\n    timeout = int(env.get('REPO_TIMEOUT_MS', '2500'))\n    if timeout <= 0:\n        raise ValueError('positive timeout required')\n    return {'timeout_ms': timeout}\n",
        "client.py": "from settings import load_settings\n\ndef request_options(env):\n    return {'timeout_seconds': load_settings(env)['timeout_ms'] / 1000}\n",
    },
    "stable-deduplicate": {
        "records.py": "def identity(value):\n    return value\n\ndef unique_by(items, key):\n    result, seen = [], set()\n    for item in items:\n        value = key(item)\n        if value not in seen:\n            seen.add(value)\n            result.append(item)\n    return result\n",
    },
    "explain-timeout": {},
}


class ScriptedModel(MessageModel):
    def __init__(self, commands, answer="Done"):
        super().__init__()
        self.commands = iter(commands)
        self.answer = answer

    def query(self, messages):
        return self.format_message(self.answer, [{"command": next(self.commands)}])


def scripted_model(case, changes=None, answer="Done"):
    commands = [f"cat {shlex.quote(path)}" for path in case["files"]]
    if changes:
        source = "from pathlib import Path\n" + "\n".join(
            f"Path({path!r}).write_text({content!r}, encoding='utf-8')" for path, content in changes.items()
        )
        commands.append(shlex.join([sys.executable, "-c", source]))
    commands.append("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
    return ScriptedModel(commands, answer)


def reference_changes(case):
    changes = dict(SOLUTIONS[case["id"]])
    if case["category"] != "read-only":
        changes["tests/test_regression.py"] = (
            "import unittest\n\nclass RegressionTests(unittest.TestCase):\n"
            "    def test_behavior(self):\n" + textwrap.indent(case["acceptance"], "        ")
        )
    return changes


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_cases_have_expected_baseline_and_accept_correct_solutions(tmp_path, case):
    result = run_case(case, tmp_path / "run", lambda: scripted_model(
        case, reference_changes(case), "REPO_TIMEOUT defaults to 30; invalid values raise ValueError.",
    ))
    assert result["baseline"]["passed"] == case["baseline_passes"]
    assert result["passed"], result
    assert not result["false_completion"]
    assert (tmp_path / "run" / "trajectory.json").is_file()
    assert (tmp_path / "run" / "workspace").is_dir()


def test_agent_completion_is_not_a_passing_grade(tmp_path):
    case = CASES[0]
    result = run_case(case, tmp_path / "run", lambda: scripted_model(case))
    assert result["agent_status"] == "completed"
    assert result["false_completion"]
    assert not result["passed"]
    assert result["acceptance"]["status"] == "failed"
    assert result["summary_coverage"]["missing_subjects"] == ["paginate", "pagination.py"]
    assert result["handoff"]["verification"]["state"] == "not_configured"


@pytest.mark.parametrize("answer, status, evaluated, passed", [
    ("Resubmitting after checking README.", "completed", True, False),
    ("Implemented unique_by in records.py; tests passed.", "completed", True, True),
    ("unique_by in records.py is unfinished.", "max_steps", False, False),
])
def test_summary_subject_coverage_is_only_a_completed_answer_diagnostic(answer, status, evaluated, passed):
    review = summary_coverage(CASES[2], answer, status)
    assert review["evaluated"] is evaluated
    assert review["passed"] is passed
    assert "does not verify claims" in review["scope"]


def test_summary_coverage_does_not_change_code_grading(tmp_path):
    case = CASES[0]
    result = run_case(case, tmp_path / "run", lambda: scripted_model(case, reference_changes(case), "Done"))
    assert result["passed"]
    assert not result["summary_coverage"]["passed"]
    metrics = summarize([result])
    assert metrics["summaries_evaluated"] == 1
    assert metrics["summaries_with_required_subjects"] == 0
    assert metrics["summary_subject_coverage_rate"] == 0


def test_changing_visible_tests_does_not_bypass_acceptance(tmp_path):
    case = CASES[0]
    result = run_case(case, tmp_path / "run", lambda: scripted_model(
        case, {"tests/test_pagination.py": "# All tests removed\n"},
    ))
    assert result["false_completion"]
    assert not result["acceptance"]["passed"]


def test_unrelated_edit_fails_even_with_correct_code(tmp_path):
    case = CASES[0]
    result = run_case(case, tmp_path / "run", lambda: scripted_model(
        case, {**reference_changes(case), "README.md": "overwritten"},
    ))
    assert result["acceptance"]["passed"]
    assert result["unrelated_changes"] == ["README.md"]
    assert result["false_completion"]


def test_correct_code_without_requested_tests_is_not_complete(tmp_path):
    case = CASES[0]
    result = run_case(case, tmp_path / "run", lambda: scripted_model(case, SOLUTIONS[case["id"]]))
    assert result["acceptance"]["passed"]
    assert not result["public_tests"]["passed"]
    assert result["false_completion"]


def test_configured_protection_rejects_unrelated_edit_before_completion(tmp_path):
    case = CASES[0]
    commands_model = scripted_model(case, {**reference_changes(case), "README.md": "unrelated"})
    result = run_case(case, tmp_path / "run", lambda: commands_model, max_steps=len(case["files"]) + 2,
                      protected_paths=("README.md",))
    assert result["agent_status"] == "max_steps"
    assert not result["false_completion"]
    assert result["acceptance"]["passed"]
    assert result["unrelated_changes"] == ["README.md"]


@pytest.mark.parametrize("correct", [True, False])
def test_evaluation_required_checks_are_applied_and_saved(tmp_path, correct):
    case = CASES[0]
    check = shlex.join([sys.executable, "-B", "-c",
                        "from pagination import paginate; assert paginate([1, 2, 3], 1, 2) == [1, 2]"])
    result = run_case(case, tmp_path / "run", lambda: scripted_model(
        case, reference_changes(case) if correct else None),
        max_steps=len(case["files"]) + (2 if correct else 1), verification_commands=(check,))
    assert result["passed"] is correct
    assert result["agent_status"] == ("completed" if correct else "max_steps")
    assert not result["false_completion"]
    trajectory = json.loads((tmp_path / "run" / "trajectory.json").read_text())
    assert trajectory["component_config"]["agent"]["verification_commands"] == [check]
    assert trajectory["verifications"][-1]["status"] == ("success" if correct else "failed")


def test_cli_records_explicit_checks_and_rejects_empty_checks(tmp_path):
    output = tmp_path / "with-checks"
    assert main(["--case", CASES[0]["id"], "--max-steps", "2", "--verify", "true",
                 "--verify", "false", "--output", str(output)]) == 1
    summary = json.loads((output / "summary.json").read_text())
    assert summary["configuration"]["verification_commands"] == ["true", "false"]
    invalid = tmp_path / "invalid"
    with pytest.raises(SystemExit) as exc:
        main(["--verify", " ", "--output", str(invalid)])
    assert exc.value.code == 2
    assert not invalid.exists()


def test_read_only_task_requires_answer_facts_and_unchanged_files(tmp_path):
    case = CASES[-1]
    missing_answer = run_case(case, tmp_path / "missing", lambda: scripted_model(case))
    assert missing_answer["missing_answer_facts"] == case["answer_contains"]
    assert not missing_answer["passed"]
    changed = run_case(case, tmp_path / "changed", lambda: scripted_model(
        case, {"new.txt": "unexpected"}, "REPO_TIMEOUT 30 ValueError",
    ))
    assert changed["missing_answer_facts"] == []
    assert changed["unrelated_changes"] == ["new.txt"]
    assert not changed["passed"]


def test_provider_failure_is_reported_without_losing_artifacts(tmp_path):
    def failing_factory():
        raise RuntimeError("provider offline")

    result = run_case(CASES[0], tmp_path / "run", failing_factory)
    assert result["agent_status"] == "error"
    assert "provider offline" in result["error"]
    assert json.loads((tmp_path / "run" / "result.json").read_text())["error"] == result["error"]


def test_acceptance_cannot_pass_by_exiting_early(tmp_path):
    case = {"acceptance": "raise SystemExit(0)\n"}
    assert acceptance_check(case, tmp_path, 1)["passed"] is False


def test_acceptance_has_a_timeout(tmp_path):
    result = acceptance_check({"acceptance": "import time\ntime.sleep(10)\n"}, tmp_path, .05)
    assert not result["passed"]
    assert result["status"] == "timed_out"


def test_cli_repeats_and_preserves_reports_without_api_key(tmp_path):
    output = tmp_path / "results"
    assert main(["--case", CASES[0]["id"], "--repeat", "2", "--max-steps", "2", "--output", str(output)]) == 1
    summary = json.loads((output / "summary.json").read_text())
    assert summary["summary"]["runs"] == 2
    assert summary["summary"]["passed"] == 0
    assert [result["attempt"] for result in summary["results"]] == [1, 2]
    assert len(summary["suite_sha256"]) == 64
    with pytest.raises(SystemExit):
        main(["--output", str(output)])
    assert json.loads((output / "summary.json").read_text()) == summary


def test_cli_lists_cases_without_creating_output(tmp_path, capsys):
    output = tmp_path / "unused"
    assert main(["--list", "--output", str(output)]) == 0
    assert "pagination-boundary" in capsys.readouterr().out
    assert not output.exists()


def test_metrics_use_completed_runs_as_false_completion_denominator():
    results = [
        {"agent_status": status, "passed": passed, "false_completion": false_completion,
         "steps": 2, "elapsed_seconds": 1, "unrelated_changes": []}
        for status, passed, false_completion in [
            ("completed", True, False), ("completed", False, True), ("max_steps", False, False),
        ]
    ]
    summary = summarize(results)
    assert summary["pass_rate"] == 1 / 3
    assert summary["false_completion_rate"] == .5
    assert summarize([])["runs"] == 0
