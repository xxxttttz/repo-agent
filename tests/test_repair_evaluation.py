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


def test_timeout_contract_is_public_protected_and_does_not_modify_core_case():
    from repo_agent.evaluation.runner import load_cases

    case = next(c for c in CASES if c["id"] == "timeout-units")
    original = next(c for c in load_cases() if c["id"] == "timeout-units")
    assert "returns only the timeout_seconds key" in case["task"]
    assert "Do not add extra keys" in case["task"]
    assert "tests/test_api_contract.py" in case["protected_paths"]
    assert case["public_contract_version"] == "timeout-contract-v2"
    assert case["minimum_tests"] == 7
    assert "ci/check_regressions.py" in case["protected_paths"]
    assert case["regression_gate_version"] == "runnable-regression-v1"
    assert "tests/test_api_contract.py" not in original["files"]
    assert "Do not add extra keys" not in original["task"]
    assert case["acceptance"] == original["acceptance"]


@pytest.mark.parametrize("changed_api", ["settings", "client"])
def test_public_contract_rejects_extra_return_keys_before_candidate_delivery(tmp_path, monkeypatch, changed_api):
    case = next(c for c in CASES if c["id"] == "timeout-units")
    modified = changes(case)
    if changed_api == "client":
        modified["client.py"] = (
            "from settings import load_settings\n\ndef request_options(env):\n"
            "    ms = load_settings(env)['timeout_ms']\n"
            "    return {'timeout_seconds': ms / 1000, 'timeout_ms': ms}\n"
        )
    else:
        modified["settings.py"] = modified["settings.py"].replace(
            "return {'timeout_ms': timeout}", "return {'timeout_ms': timeout, 'extra': True}")
    # One attempt ending at the rejected marker, without scripted-model exhaustion.
    budget = len(case["files"]) + len(modified) + 2
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: ScriptedRepairModel(case, modified))
    result = run_repair_case(case, tmp_path / "run", max_steps=budget)
    assert result["task_status"] == "max_steps", result
    assert not result["review_ready"] and not result["passed"]
    assert result["source_unchanged"]
    assert result["public_contract_version"] == "timeout-contract-v2"
    assert not (tmp_path / "run/candidate.patch").exists()
    trajectory = json.loads((tmp_path / "run/trajectory.json").read_text())
    assert "Required verification failed" in trajectory["steps"][-1]["completion_rejection"]
    assert "test_api_contract" in trajectory["verifications"][0]["output"]
    assert trajectory["verifications"][0]["returncode"] == 1


def test_fixed_contract_tests_and_empty_regression_are_rejected_before_candidate(tmp_path, monkeypatch):
    case = next(c for c in CASES if c["id"] == "timeout-units")
    modified = {**SOLUTIONS[case["id"]], "tests/test_regression_empty.py": "# No model-added tests\n"}
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: ScriptedRepairModel(case, modified))
    budget = len(case["files"]) + len(modified) + 2
    result = run_repair_case(case, tmp_path / "run", max_steps=budget)
    assert result["task_status"] == "max_steps"
    assert not result["review_ready"] and result["acceptance"]["passed"]
    assert "Ran 6 tests" in result["public_tests"]["output"]
    assert not result["public_tests"]["passed"]
    assert not result["false_candidate"] and not result["passed"]
    trajectory = json.loads((tmp_path / "run/trajectory.json").read_text())
    assert trajectory["verifications"][0]["returncode"] == 0
    assert "non-skipped" in trajectory["verifications"][1]["output"]
    assert not (tmp_path / "run/candidate.patch").exists()


@pytest.mark.parametrize("bad_validation", ["unit-only", "accept-zero", "accept-negative", "accept-invalid"])
def test_public_contract_rejects_incomplete_timeout_validation(tmp_path, monkeypatch, bad_validation):
    case = next(c for c in CASES if c["id"] == "timeout-units")
    modified = changes(case)
    if bad_validation == "unit-only":
        modified["settings.py"] = case["files"]["settings.py"]
    elif bad_validation == "accept-zero":
        modified["settings.py"] = modified["settings.py"].replace("timeout <= 0", "timeout < 0")
    elif bad_validation == "accept-negative":
        modified["settings.py"] = modified["settings.py"].replace("timeout <= 0", "timeout == 0")
    else:
        modified["settings.py"] = modified["settings.py"].replace(
            "    timeout = int(env.get('REPO_TIMEOUT_MS', '2500'))",
            "    try:\n        timeout = int(env.get('REPO_TIMEOUT_MS', '2500'))\n"
            "    except ValueError:\n        timeout = 2500")
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: ScriptedRepairModel(case, modified))
    result = run_repair_case(case, tmp_path / "run", max_steps=len(case["files"]) + len(modified) + 2)
    assert result["task_status"] == "max_steps"
    assert not result["review_ready"] and not result["passed"]
    assert result["source_unchanged"]
    trajectory = json.loads((tmp_path / "run/trajectory.json").read_text())
    assert "Required verification failed" in trajectory["steps"][-1]["completion_rejection"]
    assert "reject_invalid_timeouts" in trajectory["verifications"][0]["output"]
    assert trajectory["verifications"][0]["returncode"] == 1
    assert not (tmp_path / "run/candidate.patch").exists()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("decorator", ["@unittest.skip('not implemented')", "@unittest.expectedFailure"])
def test_nonpassing_only_regression_is_rejected_before_candidate(tmp_path, monkeypatch, case, decorator):
    modified = {**SOLUTIONS[case["id"]], "tests/test_regression_skipped.py": (
        "import unittest\n\nclass Regression(unittest.TestCase):\n"
        f"    {decorator}\n    def test_behavior(self):\n        self.fail('not implemented')\n"
    )}
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: ScriptedRepairModel(case, modified))
    result = run_repair_case(case, tmp_path / "run", max_steps=len(case["files"]) + len(modified) + 2)
    assert result["task_status"] == "max_steps" and not result["review_ready"]
    trajectory = json.loads((tmp_path / "run/trajectory.json").read_text())
    assert trajectory["verifications"][0]["returncode"] == 0
    assert trajectory["verifications"][1]["returncode"] == 1
    assert "non-skipped" in trajectory["verifications"][1]["output"]
    assert not (tmp_path / "run/candidate.patch").exists()


def test_model_cannot_disable_the_protected_regression_gate(tmp_path, monkeypatch):
    case = CASES[0]
    modified = SOLUTIONS[case["id"]]

    def tampering_model(config):
        model = ScriptedRepairModel(case, modified)
        commands = list(model.commands)
        commands.insert(len(case["files"]), "printf 'import sys; sys.exit(0)\\n' > ci/check_regressions.py")
        model.commands = iter(commands)
        return model

    monkeypatch.setattr("repo_agent.service.get_model", tampering_model)
    result = run_repair_case(case, tmp_path / "run", max_steps=len(case["files"]) + len(modified) + 3)
    assert result["task_status"] == "max_steps" and not result["review_ready"]
    assert result["source_unchanged"]
    trajectory = json.loads((tmp_path / "run/trajectory.json").read_text())
    assert "ci/check_regressions.py" in trajectory["steps"][-1]["completion_rejection"]
    assert trajectory["verifications"] == []
    assert not (tmp_path / "run/candidate.patch").exists()


def test_runnable_regression_with_another_skipped_test_is_accepted(tmp_path, monkeypatch):
    case = next(c for c in CASES if c["id"] == "timeout-units")
    modified = changes(case)
    modified["tests/test_regression_skipped.py"] = (
        "import unittest\n\nclass Skipped(unittest.TestCase):\n"
        "    @unittest.skip('optional')\n    def test_optional(self):\n        pass\n"
    )
    monkeypatch.setattr("repo_agent.service.get_model", lambda config: ScriptedRepairModel(case, modified))
    result = run_repair_case(case, tmp_path / "run")
    assert result["passed"], result
    assert result["regression_gate_version"] == "runnable-regression-v1"
    checks = result["post_verification"]["checks"]
    assert len(checks) == 3
    assert "REGRESSION_TESTS_PASSED" in checks[1]["output"]


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
    result = run_repair_case(case, tmp_path / "no-tests", max_steps=len(case["files"]) + len(SOLUTIONS[case["id"]]) + 2)
    assert result["task_status"] == "max_steps"
    assert not result["review_ready"]
    assert result["acceptance"]["passed"]
    assert not result["public_tests"]["passed"]
    assert not result["false_candidate"]
    assert not (tmp_path / "no-tests/candidate.patch").exists()


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
