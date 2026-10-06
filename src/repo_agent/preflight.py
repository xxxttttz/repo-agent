"""Model-free, opt-in local checks before connecting a trusted repository."""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .repairs import run_checks


def _git(workspace: Path, *args: str, optional: bool = False) -> str | None:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(workspace), *args],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if optional and result.returncode == 1:
        return None
    if result.returncode:
        raise RuntimeError("Could not inspect repository with Git")
    return result.stdout.rstrip("\n")


def _snapshot(workspace: Path) -> dict:
    return {
        "head": _git(workspace, "rev-parse", "HEAD"),
        "branch": _git(workspace, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True),
        "status_entries": _git(workspace, "status", "--porcelain=v1", "--untracked-files=all").splitlines(),
    }


def inspect_project(workspace: str | Path, commands: tuple[str, ...] = (), *,
                    timeout: float = 30, include_logs: bool = False) -> dict:
    """Inspect Git, then run explicitly selected trusted checks in the source.

    This is not a sandbox or a repair operation. Commands can write files; source
    observations detect some changes, never undo them or claim full isolation.
    """
    if (type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 300):
        raise ValueError("Timeout must be finite and between 0 and 300 seconds")
    if (not isinstance(commands, (list, tuple)) or len(commands) > 20
            or any(not isinstance(command, str) or not command.strip() for command in commands)):
        raise ValueError("Provide at most 20 non-empty check commands")
    workspace = Path(workspace).expanduser().resolve()
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "workspace": str(workspace),
        "runner_python": platform.python_version(),
        "state": "error",
        "summary": "无法检查项目。",
        "source_before": None,
        "source_after": None,
        "source_observations_unchanged": None,
        "baseline": {"state": "not_run", "checks": []},
        "model_invoked": False,
        "limitations": [
            "Checks run locally in the source workspace, not in an OS sandbox or isolated worktree.",
            "Only explicitly selected trusted commands run; no dependency installation or automatic repair.",
            "Git HEAD/branch/status observations exclude ignored files and cannot detect transient restored changes.",
            "Passing selected checks does not prove all behavior, test coverage or parity with remote CI.",
            "Check failure alone does not distinguish a code defect from dependency/configuration problems.",
            "Commands and workspace paths are not redacted; raw logs are omitted unless explicitly requested.",
        ],
    }
    try:
        if not workspace.is_dir():
            report["summary"] = "项目目录不存在。"
            return report
        root = Path(_git(workspace, "rev-parse", "--show-toplevel")).resolve()
        if root != workspace:
            report.update(state="blocked", summary="请指定 Git 仓库根目录，而不是子目录。")
            return report
        before = _snapshot(workspace)
        report["source_before"] = before
        report["project_files"] = [name for name in (
            "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini", "tox.ini",
        ) if (workspace / name).is_file()]
        if before["status_entries"] or before["branch"] is None:
            report.update(state="blocked", summary="仓库有未提交改动或处于 detached HEAD；未执行测试，也未改动或清理文件。")
            return report
        if not commands:
            report.update(state="not_configured", summary="Git 仓库检查完成；尚未指定测试命令，不能认定测试通过。")
            report["baseline"]["state"] = "not_configured"
            return report
        try:
            baseline = run_checks(workspace, commands, {"inherit_env": False},
                                  timeout=timeout, cancelled=lambda: False)
            checks = []
            for check in baseline["checks"]:
                item = {key: check[key] for key in ("command", "status", "returncode", "truncated")}
                if include_logs:
                    item.update(output=check["output"], error=check["error"])
                checks.append(item)
            report["baseline"] = {"state": baseline["state"], "checks": checks}
            state, summary = {
                "passed": ("not_reproduced", "所选检查全部通过，未发现可复现故障；未调用模型或进入修复流程。"),
                "failed": ("checks_failed", "检查失败；请审查失败输出，区分代码缺陷与依赖或环境问题。未自动修复。"),
            }.get(baseline["state"], ("error", "检查无法正常执行或超时，不能认定测试通过。"))
            report.update(state=state, summary=summary)
        finally:
            after = _snapshot(workspace)
            report["source_after"] = after
            report["source_observations_unchanged"] = before == after
            if before != after:
                report.update(state="source_changed", summary="检查期间 Git 状态发生变化；请人工检查，未自动清理或回滚。")
    except (OSError, RuntimeError, UnicodeError, subprocess.SubprocessError) as error:
        report.update(state="error", summary="Git 检查或执行器异常，不能认定项目已接入或测试通过。",
                      error_type=type(error).__name__)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect a trusted Git project without a model; checks are opt-in.")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="append", default=[], help="Trusted local check command; repeatable")
    parser.add_argument("--timeout", type=float, default=30, help="Per-command timeout, at most 300 seconds")
    parser.add_argument("--include-logs", action="store_true", help="Include raw outputs; review for secrets before sharing")
    args = parser.parse_args(argv)
    try:
        report = inspect_project(args.workspace, tuple(args.check), timeout=args.timeout, include_logs=args.include_logs)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {"not_reproduced": 0, "checks_failed": 1}.get(report["state"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
