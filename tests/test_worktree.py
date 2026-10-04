import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from repo_agent.api import TaskManager, TaskRequest
from repo_agent.worktree import WorktreeError, WorktreeManager


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    (path / "src").mkdir()
    (path / "src" / "example.py").write_text("value = 1\n", encoding="utf-8")
    git(path, "add", ".")
    git(
        path,
        "-c",
        "user.name=Test User",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "initial",
    )
    return path


def test_worktree_lifecycle_preserves_committed_branch(tmp_path):
    repo = create_repo(tmp_path / "project")
    manager = WorktreeManager(tmp_path / "managed")

    worktree = manager.create(repo / "src", "task-one")

    assert worktree.repo_root == repo
    assert worktree.source_workspace == repo / "src"
    assert worktree.workspace == worktree.path / "src"
    assert worktree.workspace.is_dir()

    (worktree.workspace / "generated.py").write_text("answer = 42\n", encoding="utf-8")
    commit = manager.commit_all(worktree, "repo-agent: generate file")

    assert commit == manager.head_commit(worktree)
    manager.remove(worktree, force=True, delete_branch=False)
    assert not worktree.path.exists()
    assert git(repo, "rev-parse", worktree.branch) == commit
    assert git(repo, "show", f"{commit}:src/generated.py") == "answer = 42"


def test_worktree_without_changes_deletes_temporary_branch(tmp_path):
    repo = create_repo(tmp_path / "project")
    manager = WorktreeManager(tmp_path / "managed")
    worktree = manager.create(repo, "task-two")

    assert manager.commit_all(worktree, "unused") is None
    manager.remove(worktree, force=True, delete_branch=True)

    branches = git(repo, "branch", "--list", worktree.branch)
    assert branches == ""
    assert not worktree.path.exists()


def test_duplicate_task_does_not_remove_existing_worktree(tmp_path):
    repo = create_repo(tmp_path / "project")
    manager = WorktreeManager(tmp_path / "managed")
    worktree = manager.create(repo, "duplicate")

    with pytest.raises(WorktreeError, match="already exists"):
        manager.create(repo, "duplicate")

    assert worktree.path.is_dir()
    assert git(repo, "rev-parse", worktree.branch) == worktree.base_commit
    manager.remove(worktree, force=True, delete_branch=True)


def test_worktree_rejects_uncommitted_source_changes(tmp_path):
    repo = create_repo(tmp_path / "project")
    manager = WorktreeManager(tmp_path / "managed")
    (repo / "uncommitted.txt").write_text("not committed\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="commit or stash"):
        manager.create(repo, "dirty-source")


def test_task_branch_merges_into_source(tmp_path):
    repo = create_repo(tmp_path / "project")
    manager = WorktreeManager(tmp_path / "managed")
    worktree = manager.create(repo, "merge-result")
    (worktree.path / "merged.txt").write_text("merged\n", encoding="utf-8")
    task_commit = manager.commit_all(worktree, "task result")

    merge_commit = manager.merge_into_source(worktree)
    manager.remove(worktree, force=True, delete_branch=True)

    assert task_commit == merge_commit
    assert (repo / "merged.txt").read_text(encoding="utf-8") == "merged\n"
    assert git(repo, "branch", "--list", worktree.branch) == ""


def test_merge_conflict_is_aborted_and_task_branch_is_preserved(tmp_path):
    repo = create_repo(tmp_path / "project")
    manager = WorktreeManager(tmp_path / "managed")
    first = manager.create(repo, "first-result")
    second = manager.create(repo, "second-result")

    (first.workspace / "src" / "example.py").write_text(
        "value = 2\n", encoding="utf-8"
    )
    (second.workspace / "src" / "example.py").write_text(
        "value = 3\n", encoding="utf-8"
    )
    manager.commit_all(first, "first result")
    manager.commit_all(second, "second result")
    manager.merge_into_source(first)

    with pytest.raises(WorktreeError, match="Could not merge"):
        manager.merge_into_source(second)

    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "src" / "example.py").read_text(encoding="utf-8") == "value = 2\n"
    assert git(repo, "branch", "--list", second.branch) != ""
    manager.remove(first, force=True, delete_branch=True)
    manager.remove(second, force=True, delete_branch=False)


def test_task_manager_executes_git_task_in_worktree(tmp_path, monkeypatch):
    repo = create_repo(tmp_path / "project")
    execution_paths = []

    def controlled_execute(spec, *, cache=None):
        execution_paths.append(spec.workspace)
        assert spec.verification_commands == ["test -d src"]
        assert spec.protected_paths == ["src/example.py"]
        (spec.workspace / "generated.txt").write_text("isolated\n", encoding="utf-8")
        return SimpleNamespace(
            trajectory={
                "status": "completed",
                "answer": "done",
                "messages": [],
                "steps": [],
            },
            index={"files": 1, "chunks": 1, "cache_hits": 0, "cache_misses": 0},
        )

    monkeypatch.setattr("repo_agent.api.execute_task", controlled_execute)
    manager = TaskManager(
        tmp_path,
        redis_url=None,
        worktree_root=tmp_path / "managed",
    )
    accepted = manager.submit(
        TaskRequest(task="Generate a file", workspace="project", provider="mock",
                    verification_commands=["test -d src"], protected_paths=["src/example.py"],
                    delivery_mode="auto_merge")
    )

    for _ in range(300):
        record = manager.get(accepted["id"])
        if record["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("task did not finish")

    manager.close()
    assert record["status"] == "completed"
    assert record["worktree_commit"] is not None
    assert record["worktree_merged"] is True
    assert record["worktree_merge_commit"] == record["worktree_commit"]
    assert record["worktree_merge_error"] is None
    assert record["worktree_cleaned"] is True
    assert not Path(record["worktree_path"]).exists()
    assert execution_paths == [Path(record["execution_workspace"])]
    assert execution_paths[0] != repo
    assert (repo / "generated.txt").read_text(encoding="utf-8") == "isolated\n"
    assert git(repo, "show", f"{record['worktree_commit']}:generated.txt") == "isolated"
    assert git(repo, "branch", "--list", record["worktree_branch"]) == ""


@pytest.mark.parametrize("task_status", ["max_steps", "cancelled", "error", "exception"])
def test_unfinished_task_preserves_uncommitted_work(tmp_path, monkeypatch, task_status):
    repo = create_repo(tmp_path / "project")

    def controlled_execute(spec, *, cache=None):
        (spec.workspace / "partial.txt").write_text("valuable progress\n", encoding="utf-8")
        if task_status == "exception":
            raise RuntimeError("provider crashed")
        return SimpleNamespace(
            trajectory={"status": task_status, "answer": "unfinished", "messages": [], "steps": []},
            index={},
        )

    monkeypatch.setattr("repo_agent.api.execute_task", controlled_execute)
    manager = TaskManager(tmp_path, redis_url=None, worktree_root=tmp_path / "managed")
    try:
        accepted = manager.submit(TaskRequest(task="Implement feature", workspace="project", provider="mock"))
        for _ in range(300):
            record = manager.get(accepted["id"])
            if record["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("task did not finish")
        assert record["status"] == ("error" if task_status == "exception" else task_status)
        assert record["worktree_cleaned"] is False
        assert (Path(record["worktree_path"]) / "partial.txt").read_text() == "valuable progress\n"
        assert not (repo / "partial.txt").exists()
        assert git(repo, "branch", "--list", record["worktree_branch"])
    finally:
        manager.close()
