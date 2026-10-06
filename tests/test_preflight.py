import json
import shlex
import subprocess
import sys

import pytest

from repo_agent.preflight import inspect_project, main


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text("[project]\nname = 'example'\nversion = '0.1.0'\n")
    (repo / ".gitignore").write_text("cache/\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "baseline")
    return repo


def command(code):
    return shlex.join([sys.executable, "-B", "-c", code])


def test_default_inspection_never_executes_checks_or_loads_a_model(project, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Inspection must not execute checks or load a model")

    monkeypatch.setattr("repo_agent.preflight.run_checks", forbidden)
    monkeypatch.setattr("repo_agent.models.get_model", forbidden)
    report = inspect_project(project)
    assert report["state"] == "not_configured"
    assert report["baseline"] == {"state": "not_configured", "checks": []}
    assert report["project_files"] == ["pyproject.toml"]
    assert report["source_before"]["status_entries"] == []
    assert report["source_after"] is None
    assert report["source_observations_unchanged"] is None
    assert report["model_invoked"] is False


def test_passing_checks_do_not_repair_commit_or_load_a_model(project, monkeypatch):
    monkeypatch.setattr("repo_agent.models.get_model", lambda *args, **kwargs: pytest.fail("No model"))
    before = git(project, "rev-parse", "HEAD")
    report = inspect_project(project, (command("print('healthy')"), "git diff --check"))
    assert report["state"] == "not_reproduced"
    assert report["baseline"]["state"] == "passed"
    assert len(report["baseline"]["checks"]) == 2
    assert "未发现可复现故障" in report["summary"]
    assert report["source_observations_unchanged"] is True
    assert git(project, "rev-parse", "HEAD") == before
    assert git(project, "status", "--porcelain") == ""
    assert git(project, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_failed_check_is_not_automatically_a_code_defect(project):
    report = inspect_project(project, (command("raise SystemExit(1)"),))
    assert report["state"] == "checks_failed"
    assert report["baseline"]["state"] == "failed"
    assert report["source_observations_unchanged"] is True
    assert report["model_invoked"] is False


@pytest.mark.parametrize("check", ["repo_agent_nonexistent_command", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                                    command("import time; time.sleep(1)")])
def test_missing_command_submission_marker_and_timeout_are_not_failures_to_repair(project, check):
    report = inspect_project(project, (check,), timeout=.1)
    assert report["state"] == "error"
    assert report["baseline"]["state"] == "error"
    assert report["source_observations_unchanged"] is True


@pytest.mark.parametrize("change", ["untracked", "unstaged", "staged", "detached"])
def test_dirty_or_detached_source_blocks_before_any_check(project, monkeypatch, change):
    if change == "detached":
        git(project, "checkout", "--detach", "-q")
    elif change == "untracked":
        (project / "user.txt").write_text("user work")
    else:
        (project / "pyproject.toml").write_text("# user work\n")
        if change == "staged":
            git(project, "add", "pyproject.toml")
    before = git(project, "status", "--porcelain")
    monkeypatch.setattr("repo_agent.preflight.run_checks", lambda *args, **kwargs: pytest.fail("No execution"))
    report = inspect_project(project, ("true",))
    assert report["state"] == "blocked"
    assert report["baseline"]["state"] == "not_run"
    assert git(project, "status", "--porcelain") == before


@pytest.mark.parametrize("code", [
    "from pathlib import Path; Path('pyproject.toml').write_text('# changed')",
    "from pathlib import Path; Path('extra.txt').write_text('new')",
])
def test_check_that_changes_source_is_reported_without_cleanup(project, code):
    report = inspect_project(project, (command(code),))
    assert report["state"] == "source_changed"
    assert report["baseline"]["state"] == "passed"
    assert report["source_observations_unchanged"] is False
    assert report["source_after"]["status_entries"]
    assert git(project, "status", "--porcelain")


def test_branch_change_is_detected_even_if_working_files_are_clean(project):
    report = inspect_project(project, ("git switch -q -c another-branch",))
    assert report["state"] == "source_changed"
    assert report["source_before"]["head"] == report["source_after"]["head"]
    assert report["source_after"]["status_entries"] == []
    assert git(project, "symbolic-ref", "--short", "HEAD") == "another-branch"


def test_committed_change_is_detected_even_if_working_files_are_clean(project):
    check = (command("from pathlib import Path; Path('extra.txt').write_text('new')")
             + " && git add extra.txt && git -c user.name=Test -c user.email=test@example.com commit -qm check")
    report = inspect_project(project, (check,))
    assert report["state"] == "source_changed"
    assert report["source_after"]["status_entries"] == []
    assert report["source_before"]["branch"] == report["source_after"]["branch"]
    assert report["source_before"]["head"] != report["source_after"]["head"]
    assert (project / "extra.txt").exists()


def test_executor_exception_still_observes_source_afterwards(project, monkeypatch):
    def broken(*args, **kwargs):
        (project / "extra.txt").write_text("preserve this")
        raise OSError("executor unavailable")

    monkeypatch.setattr("repo_agent.preflight.run_checks", broken)
    report = inspect_project(project, ("true",))
    assert report["state"] == "error"
    assert report["error_type"] == "OSError"
    assert report["source_observations_unchanged"] is False
    assert report["baseline"]["state"] == "not_run"
    assert (project / "extra.txt").read_text() == "preserve this"


def test_ignored_files_are_an_explicit_limit_not_a_cleanliness_guarantee(project):
    report = inspect_project(project, (command("from pathlib import Path; Path('cache').mkdir()"),))
    assert report["state"] == "not_reproduced"
    assert report["source_observations_unchanged"] is True
    assert (project / "cache").is_dir()
    assert any("ignored files" in item for item in report["limitations"])


def test_logs_are_opt_in_and_credentials_not_inherited(project, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "preflight-test-secret")
    check = command("import os; assert 'HF_TOKEN' not in os.environ; print('private-output')")
    report = inspect_project(project, (check,))
    assert report["state"] == "not_reproduced"
    assert "output" not in report["baseline"]["checks"][0]
    assert "error" not in report["baseline"]["checks"][0]
    with_logs = inspect_project(project, (check,), include_logs=True)
    assert with_logs["baseline"]["checks"][0]["output"] == "private-output\n"
    assert "preflight-test-secret" not in json.dumps(with_logs)


def test_invalid_directory_non_git_and_subdirectory_never_execute(tmp_path, project, monkeypatch):
    monkeypatch.setattr("repo_agent.preflight.run_checks", lambda *args, **kwargs: pytest.fail("No execution"))
    assert inspect_project(tmp_path / "missing", ("true",))["state"] == "error"
    assert inspect_project(tmp_path, ("true",))["state"] == "error"
    subdir = project / "cache"
    subdir.mkdir()
    assert inspect_project(subdir, ("true",))["state"] == "blocked"


@pytest.mark.parametrize("timeout", [0, -1, 301, float("nan"), float("inf"), True, "30"])
def test_invalid_timeout(project, timeout):
    with pytest.raises(ValueError, match="Timeout"):
        inspect_project(project, timeout=timeout)


@pytest.mark.parametrize("commands", [[""], [" "], [None], "true", ["true"] * 21])
def test_invalid_commands(project, commands):
    with pytest.raises(ValueError, match="commands"):
        inspect_project(project, commands)


def test_git_failure_after_checks_cannot_claim_source_unchanged(project, monkeypatch):
    from repo_agent.preflight import _snapshot

    calls = []

    def snapshot(workspace):
        calls.append(workspace)
        if len(calls) == 2:
            raise subprocess.TimeoutExpired("git", 30)
        return _snapshot(workspace)

    monkeypatch.setattr("repo_agent.preflight._snapshot", snapshot)
    report = inspect_project(project, ("true",))
    assert report["state"] == "error"
    assert report["baseline"]["state"] == "passed"
    assert report["source_after"] is None
    assert report["source_observations_unchanged"] is None


@pytest.mark.parametrize("checks, expected", [([], 2), (["true"], 0), (["false"], 1),
                                           (["repo_agent_nonexistent_command"], 2)])
def test_cli_exit_codes_and_json(project, capsys, checks, expected):
    args = ["--workspace", str(project)]
    for check in checks:
        args.extend(["--check", check])
    assert main(args) == expected
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == 1
    assert report["model_invoked"] is False


def test_cli_rejects_invalid_budget(project):
    with pytest.raises(SystemExit) as error:
        main(["--workspace", str(project), "--timeout", "nan"])
    assert error.value.code == 2
