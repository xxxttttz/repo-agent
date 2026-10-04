"""Git worktree lifecycle helpers for isolated task execution."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeError(RuntimeError):
    """Raised when a managed Git worktree operation fails."""


class WorktreeMergeError(WorktreeError):
    """Raised when task changes cannot be merged into the source checkout."""


@dataclass(frozen=True, slots=True)
class TaskWorktree:
    source_workspace: Path
    repo_root: Path
    path: Path
    workspace: Path
    branch: str
    base_commit: str
    source_branch: str


class WorktreeManager:
    """Create, commit, and remove per-task Git worktrees."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def _run_git(
        self,
        repo: Path,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), *args],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise WorktreeError(f"Could not execute Git: {error}") from error

        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
            raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
        return result

    def _git(self, repo: Path, *args: str) -> str:
        return self._run_git(repo, *args).stdout.strip()

    def repo_root(self, workspace: Path) -> Path | None:
        """Return the containing repository root, or None outside a Git repository."""
        workspace = workspace.expanduser().resolve()
        result = self._run_git(
            workspace,
            "rev-parse",
            "--show-toplevel",
            check=False,
        )
        if result.returncode != 0:
            return None
        return Path(result.stdout.strip()).resolve()

    def create(self, workspace: Path, task_id: str) -> TaskWorktree:
        workspace = workspace.expanduser().resolve()
        repo_root = self.repo_root(workspace)
        if repo_root is None:
            raise WorktreeError(f"Workspace is not inside a Git repository: {workspace}")

        try:
            relative_workspace = workspace.relative_to(repo_root)
        except ValueError as error:
            raise WorktreeError(f"Workspace is outside repository root: {workspace}") from error

        status = self._git(repo_root, "status", "--porcelain", "--untracked-files=all")
        if status:
            raise WorktreeError(
                "Source repository must be clean before starting a task; "
                "commit or stash its changes first"
            )

        branch_result = self._run_git(
            repo_root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
        )
        if branch_result.returncode != 0:
            raise WorktreeError("Source repository must have a checked-out branch")
        source_branch = branch_result.stdout.strip()
        base_commit = self._git(repo_root, "rev-parse", "HEAD")
        branch = f"repo-agent/{task_id}"
        repo_key = hashlib.sha256(str(repo_root).encode()).hexdigest()[:12]
        path = self.root / f"{repo_root.name}-{repo_key}" / task_id
        if path.exists():
            raise WorktreeError(f"Task worktree path already exists: {path}")
        branch_ref = f"refs/heads/{branch}"
        branch_exists = self._run_git(
            repo_root,
            "show-ref",
            "--verify",
            "--quiet",
            branch_ref,
            check=False,
        )
        if branch_exists.returncode == 0:
            raise WorktreeError(f"Task worktree branch already exists: {branch}")
        if branch_exists.returncode != 1:
            detail = (
                branch_exists.stderr.strip()
                or branch_exists.stdout.strip()
                or "unknown Git error"
            )
            raise WorktreeError(f"Could not inspect task branch: {detail}")
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._git(
                repo_root,
                "worktree",
                "add",
                "-b",
                branch,
                str(path),
                base_commit,
            )
        except WorktreeError:
            shutil.rmtree(path, ignore_errors=True)
            self._run_git(repo_root, "worktree", "prune", check=False)
            self._run_git(repo_root, "branch", "-D", branch, check=False)
            raise

        return TaskWorktree(
            source_workspace=workspace,
            repo_root=repo_root,
            path=path,
            workspace=path / relative_workspace,
            branch=branch,
            base_commit=base_commit,
            source_branch=source_branch,
        )

    def head_commit(self, worktree: TaskWorktree) -> str:
        return self._git(worktree.path, "rev-parse", "HEAD")

    def changed_paths(self, worktree: TaskWorktree, commit: str | None = None) -> list[str]:
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", worktree.base_commit]
        if commit:
            args.append(commit)
        tracked = self._run_git(worktree.path, *args, "--").stdout.split("\0")
        untracked = [] if commit else self._run_git(
            worktree.path, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
        return sorted({path for path in tracked + untracked if path})

    def candidate(self, worktree: TaskWorktree, commit: str) -> dict:
        patch = self._run_git(worktree.path, "diff", "--binary", "--no-ext-diff", "--no-textconv",
                              "--no-renames", worktree.base_commit, commit, "--").stdout
        if len(patch.encode("utf-8")) > 1_000_000:
            raise WorktreeError("Candidate diff exceeds the 1 MB review limit")
        return {"base_commit": worktree.base_commit, "commit": commit,
                "changed_paths": self.changed_paths(worktree, commit), "diff": patch,
                "diff_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest()}

    def assert_reviewable(self, worktree: TaskWorktree, commit: str) -> None:
        if self.head_commit(worktree) != commit or self._git(worktree.path, "rev-parse", worktree.branch) != commit:
            raise WorktreeError("Candidate commit changed; generate and review a new candidate")
        if self._git(worktree.path, "symbolic-ref", "--short", "HEAD") != worktree.branch:
            raise WorktreeError("Candidate branch changed")
        if self._git(worktree.path, "status", "--porcelain", "--untracked-files=all"):
            raise WorktreeError("Candidate workspace changed after review")
        if self._git(worktree.repo_root, "rev-parse", "HEAD") != worktree.base_commit:
            raise WorktreeError("Source commit changed; repair must be rerun against the new base")
        if self._git(worktree.repo_root, "symbolic-ref", "--short", "HEAD") != worktree.source_branch:
            raise WorktreeError("Source branch changed")
        if self._git(worktree.repo_root, "status", "--porcelain", "--untracked-files=all"):
            raise WorktreeError("Source workspace has uncommitted changes")

    def approve_candidate(self, worktree: TaskWorktree, commit: str) -> str:
        """Fast-forward the exact reviewed commit; never reset on failure."""
        self.assert_reviewable(worktree, commit)
        self._git(worktree.repo_root, "merge", "--ff-only", commit)
        return self._git(worktree.repo_root, "rev-parse", "HEAD")

    def commit_all(self, worktree: TaskWorktree, message: str) -> str | None:
        """Commit all tracked and untracked changes, returning the new commit."""
        self._git(worktree.path, "add", "--all")
        staged = self._run_git(
            worktree.path,
            "diff",
            "--cached",
            "--quiet",
            check=False,
        )
        if staged.returncode == 0:
            return None
        if staged.returncode != 1:
            detail = staged.stderr.strip() or staged.stdout.strip() or "unknown Git error"
            raise WorktreeError(f"Could not inspect staged changes: {detail}")

        self._git(
            worktree.path,
            "-c",
            "user.name=Repo Agent",
            "-c",
            "user.email=repo-agent@localhost",
            "commit",
            "-m",
            message,
        )
        return self.head_commit(worktree)

    def merge_into_source(self, worktree: TaskWorktree) -> str:
        """Merge a completed task branch into its original source branch."""
        branch_result = self._run_git(
            worktree.repo_root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
        )
        current_branch = branch_result.stdout.strip()
        if branch_result.returncode != 0 or current_branch != worktree.source_branch:
            raise WorktreeMergeError(
                f"Source branch changed during task execution; expected "
                f"{worktree.source_branch!r}, found {current_branch or 'detached HEAD'!r}"
            )

        status = self._git(
            worktree.repo_root,
            "status",
            "--porcelain",
            "--untracked-files=all",
        )
        if status:
            raise WorktreeMergeError(
                "Source repository changed during task execution; "
                "commit or stash its changes before applying the task result"
            )

        pre_merge_commit = self._git(worktree.repo_root, "rev-parse", "HEAD")
        result = self._run_git(
            worktree.repo_root,
            "-c",
            "user.name=Repo Agent",
            "-c",
            "user.email=repo-agent@localhost",
            "merge",
            "--no-edit",
            worktree.branch,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "merge conflict"
            abort = self._run_git(
                worktree.repo_root,
                "merge",
                "--abort",
                check=False,
            )
            restore = self._run_git(
                worktree.repo_root,
                "reset",
                "--hard",
                pre_merge_commit,
                check=False,
            )
            cleanup_errors = []
            if abort.returncode != 0:
                cleanup_errors.append(
                    "merge abort failed: "
                    f"{abort.stderr.strip() or abort.stdout.strip()}"
                )
            if restore.returncode != 0:
                cleanup_errors.append(
                    "source restore failed: "
                    f"{restore.stderr.strip() or restore.stdout.strip()}"
                )
            if cleanup_errors:
                detail = f"{detail}; {'; '.join(cleanup_errors)}"
            raise WorktreeMergeError(f"Could not merge task result: {detail}")
        return self._git(worktree.repo_root, "rev-parse", "HEAD")

    def remove(
        self,
        worktree: TaskWorktree,
        *,
        force: bool = False,
        delete_branch: bool = False,
    ) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree.path))
        self._git(worktree.repo_root, *args)
        self._git(worktree.repo_root, "worktree", "prune")
        if delete_branch:
            self._git(worktree.repo_root, "branch", "-D", worktree.branch)
