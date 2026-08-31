"""Run Repo Agent against a local workspace."""

import argparse
import copy
import json
from pathlib import Path

from ..agents import get_agent
from ..config import get_config_from_spec, load_config
from ..environments import get_environment
from ..models import get_model
from ..result import AgentResult, AgentStatus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repo-agent", description="Run an evidence-aware coding agent locally.")
    parser.add_argument("task", nargs="?", help="Task text; optional when resuming a trajectory.")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--provider", choices=("openrouter", "groq", "huggingface", "mock"), default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--config", default=None, help="YAML config path or config name.")
    parser.add_argument("--override", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--output", type=Path, default=None, help="Save the trajectory JSON to this path.")
    parser.add_argument("--resume", type=Path, default=None, help="Continue an unfinished trajectory JSON.")
    return parser


def print_result(result: AgentResult, *, skip_steps: int = 0) -> None:
    for step in result.steps[skip_steps:]:
        if step.command is None:
            if step.completion_rejection:
                print(f"\nCompletion rejected: {step.completion_rejection}")
            continue
        print(f"\n[Step {step.number}/{result.step_count}]\nAgent wants to run:\n{step.command}\n")
        if step.execution_status is not None:
            print(f"Execution status: {step.execution_status.value}")
        print(step.output if step.output else "Execution produced no output.")
        if step.error:
            print(f"Execution error: {step.error}")
        if step.output_truncated:
            print("Output truncated: display contains only the captured limit.")
    if result.status is AgentStatus.COMPLETED:
        print(result.answer)
    elif result.status is AgentStatus.MAX_STEPS:
        new_steps = result.step_count - skip_steps
        if skip_steps:
            print(f"\nAgent stopped: used {new_steps} additional steps ({result.step_count} total) without completing the task.")
        else:
            print(f"\nAgent stopped: reached max_steps={result.step_count} without completing the task.")
    else:
        print(f"\nAgent stopped with error: {result.answer}")


def _component_configs(config: dict, args: argparse.Namespace) -> tuple[dict, dict, dict]:
    """Build independent factory configs, preserving the four-section layout."""
    agent_config = copy.deepcopy(config.get("agent", {}))
    environment_config = copy.deepcopy(config.get("environment", {}))
    model_config = copy.deepcopy(config.get("model", {}))

    for values, class_key in (
        (agent_config, "agent_class"),
        (environment_config, "environment_class"),
        (model_config, "model_class"),
    ):
        serialized_class = values.pop("class", None)
        if serialized_class is not None and class_key not in values:
            values[class_key] = serialized_class

    # Accept the old flat config shape as a temporary compatibility measure.
    if "provider" in config and "model_class" not in model_config:
        model_config["model_class"] = config["provider"]
    if "model_name" in config and "model_name" not in model_config:
        model_config["model_name"] = config["model_name"]
    if "workspace" in config and "cwd" not in environment_config:
        environment_config["cwd"] = config["workspace"]
    if "max_steps" in config and "max_steps" not in agent_config:
        agent_config["max_steps"] = config["max_steps"]

    if args.workspace is not None:
        environment_config["cwd"] = str(args.workspace)
    if args.max_steps is not None:
        agent_config["max_steps"] = args.max_steps
    if args.provider is not None:
        model_config["model_class"] = args.provider
    if args.model is not None:
        model_config["model_name"] = args.model

    agent_config.setdefault("agent_class", "default")
    environment_config.setdefault("environment_class", "local")
    environment_config.setdefault("cwd", ".")
    model_config.setdefault("model_class", "openrouter")
    return agent_config, environment_config, model_config


def _merge_config(left: dict, right: dict) -> dict:
    result = copy.deepcopy(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_config(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _drop_redacted(value):
    if isinstance(value, dict):
        return {
            key: _drop_redacted(item)
            for key, item in value.items()
            if item != "[REDACTED]"
        }
    if isinstance(value, list):
        return [_drop_redacted(item) for item in value]
    return value


def _load_trajectory(path: Path) -> dict:
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Could not load trajectory {path}: {error}") from error
    if not isinstance(data, dict):
        raise SystemExit(f"Trajectory must contain a JSON object: {path}")
    if data.get("status") == AgentStatus.COMPLETED.value:
        raise SystemExit(f"Trajectory is already completed: {path}")
    return data


def _trajectory_task(trajectory: dict) -> str | None:
    task = trajectory.get("task")
    if isinstance(task, str) and task.strip():
        return task
    prefix = "Please solve this task:\n\n"
    suffix = "\n\nInspect relevant files"
    for message in trajectory.get("messages", []):
        if message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        if content.startswith(prefix) and suffix in content:
            return content[len(prefix):].split(suffix, 1)[0]
    return None


def _config_for_run(args: argparse.Namespace, trajectory: dict | None) -> dict:
    saved_config = trajectory.get("component_config") if trajectory else None
    if args.config is None and isinstance(saved_config, dict):
        config = _drop_redacted(copy.deepcopy(saved_config))
        for override in args.override:
            config = _merge_config(config, get_config_from_spec(override))
        return config
    return load_config(args.config, args.override)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    trajectory = _load_trajectory(args.resume) if args.resume else None
    saved_task = _trajectory_task(trajectory) if trajectory else None
    if args.task is None and saved_task is None:
        raise SystemExit("A task is required unless --resume points to a trajectory containing one.")
    if trajectory and args.task is not None and saved_task is not None and args.task != saved_task:
        raise SystemExit("The supplied task does not match the task stored in the trajectory.")
    task = args.task or saved_task
    assert task is not None

    config = _config_for_run(args, trajectory)
    agent_config, environment_config, model_config = _component_configs(config, args)
    workspace = Path(environment_config["cwd"]).expanduser().resolve()
    environment_config["cwd"] = str(workspace)
    if not workspace.is_dir():
        raise SystemExit(f"Workspace is not a directory: {workspace}")
    if int(agent_config.get("max_steps", 5)) < 1:
        raise SystemExit("--max-steps must be at least 1")

    model = get_model(model_config)
    environment = get_environment(environment_config)
    component_config = {"agent": copy.deepcopy(agent_config), "environment": copy.deepcopy(environment_config),
                        "model": copy.deepcopy(model_config), "run": copy.deepcopy(config.get("run", {}))}
    agent = get_agent(model, environment, {**agent_config, "component_config": component_config})
    try:
        result = agent.resume(task, trajectory) if trajectory else agent.run(task)
    except ValueError as error:
        raise SystemExit(f"Could not resume trajectory: {error}") from error
    resumed_from_step = getattr(agent, "resumed_from_step", 0)
    print_result(result, skip_steps=resumed_from_step)
    output_path = args.output or args.resume or config.get("run", {}).get("output_path")
    if output_path:
        agent.save(output_path)
    return 0 if result.status is AgentStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
