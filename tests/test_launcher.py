import os
from pathlib import Path
import subprocess
import sys


LAUNCHER = Path(__file__).parents[1] / "repo-agent"


def launcher_env(**overrides):
    environment = os.environ.copy()
    environment.update({
        "REPO_AGENT_PYTHON": sys.executable,
        "REPO_AGENT_PROVIDER": "mock",
        **overrides,
    })
    return environment


def test_launcher_help_uses_selected_python():
    completed = subprocess.run(
        [str(LAUNCHER), "--help"],
        capture_output=True,
        check=False,
        env=launcher_env(),
        text=True,
    )
    assert completed.returncode == 0
    assert "--provider {openrouter,groq,huggingface,mock}" in completed.stdout


def test_launcher_discovers_shared_or_system_python():
    environment = launcher_env()
    environment.pop("REPO_AGENT_PYTHON")
    completed = subprocess.run(
        [str(LAUNCHER), "--help"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert completed.returncode == 0
    assert "--resume RESUME" in completed.stdout


def test_launcher_rejects_non_executable_python():
    completed = subprocess.run(
        [str(LAUNCHER), "--help"],
        capture_output=True,
        check=False,
        env=launcher_env(REPO_AGENT_PYTHON="/definitely/missing/python"),
        text=True,
    )
    assert completed.returncode == 127
    assert "Python is not executable" in completed.stderr


def test_launcher_provider_can_be_overridden_with_environment(tmp_path):
    completed = subprocess.run(
        [
            str(LAUNCHER),
            "--workspace",
            str(tmp_path),
            "--max-steps",
            "2",
            "Inspect the directory",
        ],
        capture_output=True,
        check=False,
        env=launcher_env(),
        text=True,
    )
    assert completed.returncode == 0
    assert "ls -la" in completed.stdout
    assert "Submitting the result." in completed.stdout
