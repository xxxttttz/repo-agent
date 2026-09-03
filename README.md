# Repo Agent

> 当前版本：v0.1.0（Alpha）。核心 coding-agent 流程已经可用，执行本地命令时仍应使用隔离环境并检查 trajectory。

Repo Agent 是一个精简的本地 coding agent：模型逐轮提出一个 shell 命令，环境执行并返回 observation，只有在模型使用独立提交命令且现有证据策略通过后，任务才会被接受为完成。

它适合用作本地仓库检查、轻量代码修改和 Agent 控制流实验。项目保持较小的依赖集合，不要求 Pydantic、Typer 或在线服务才能运行 Mock 流程。

## 设计边界

组件边界参考了 mini-swe-agent 的组织方式，但实现和文档是本项目自己的：

```text
repo_agent/
├── agents/        Agent 实现与动态工厂
├── environments/  本地执行环境与安全护栏
├── models/        provider 适配器、消息格式和工厂
├── config/        YAML 默认配置和 loader
└── run/           CLI 入口
```

Repo Agent 的核心特点是 evidence-aware completion：模型不能通过一句“完成了”结束任务。它必须运行独立的提交命令：

```bash
echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
```

并且任务中提到的目标文件必须已经被成功读取。提交命令输出 marker 后的文本会成为最终答案；如果没有后续文本，则使用 assistant message 的内容作为答案。

### 本地源码检索

`repo_agent.retrieval` 提供不增加第三方依赖的源码索引和 BM25 词法检索。Python
文件按顶层函数和类分块，其他支持的文本文件按行分块：

```python
from repo_agent.retrieval import BM25Retriever, build_index, format_results

chunks = build_index("./my-project")
results = BM25Retriever(chunks).search("completion policy", top_k=5)
print(format_results(results))
```

检索结果包含相对路径、符号名和行号，可作为 Agent 的候选上下文。当前版本不会自动把检索结果注入模型消息；调用方可以先筛选结果，再明确选择要提供的上下文。

## 安装

```bash
python -m pip install -e .
```

开发依赖：

```bash
python -m pip install -e '.[dev]'
```

## API key 和模型

OpenRouter 使用 `OPENROUTER_API_KEY`，Groq 使用 `GROQ_API_KEY`，Hugging Face Inference Providers 使用 `HF_TOKEN`。没有 key 时可以使用 Mock：

```bash
export OPENROUTER_API_KEY='your-key'
export GROQ_API_KEY='your-key'
export HF_TOKEN='your-token'
```

## CLI

根目录启动器会优先复用相邻 `mini-swe-agent` 的虚拟环境，因此当前目录布局不需要为 Repo Agent 再创建一份：

```bash
./repo-agent --workspace ./my-project "Inspect the project"
```

如果相邻虚拟环境不存在，启动器会回退到系统 `python3` 或 `python`。它默认使用 Hugging Face，并自动把本项目的 `src` 加入 `PYTHONPATH`。如需临时切换 provider 或 Python：

```bash
REPO_AGENT_PROVIDER=mock ./repo-agent --workspace . "Inspect the project"
REPO_AGENT_PYTHON=/path/to/python ./repo-agent --workspace . "Inspect the project"
```

安装为 Python package 后，也可以使用标准 console script：

```bash
repo-agent --provider mock --workspace . "Inspect the project"
repo-agent --provider openrouter --model z-ai/glm-5.2:free \
  --workspace ./my-project "Explain app.py"
repo-agent --provider huggingface --model Qwen/Qwen2.5-Coder-32B-Instruct:nscale \
  --workspace ./my-project "Explain app.py"
repo-agent --provider mock --output trajectory.json "Inspect the project"
```

Hugging Face provider 通过 `router.huggingface.co` 的 OpenAI-compatible Chat Completions 接口调用 Inference Providers，因此可使用 Hugging Face 账户中的适用额度。默认模型是 `Qwen/Qwen2.5-Coder-32B-Instruct:nscale`；模型名的 provider 后缀避免自动路由到当前网络无法访问的 Groq。也可使用 `HUGGINGFACEHUB_API_TOKEN` 作为兼容环境变量；`HF_TOKEN` 优先。

可用参数包括 `--workspace`、`--provider`、`--model`、`--max-steps`、`--config`、`--override` 和 `--output`。

### 恢复未完成任务

使用 `--resume` 可以从 `max_steps` 或 provider error 的 trajectory 继续，不需要重新输入原任务：

```bash
./repo-agent --resume trajectory.json --max-steps 8
```

恢复时会沿用 trajectory 中的 workspace、provider、模型配置、消息和成功命令证据；显式 CLI 参数仍可覆盖保存的配置。`--max-steps` 表示本次允许增加的步骤数，步骤编号会接着原记录递增。默认会原子更新传入的 trajectory；使用 `--output resumed.json` 可以另存结果。恢复提示会要求模型先检查当前文件，以降低 trajectory 保存后 workspace 已变化时覆盖新内容的风险，但这不是强一致性锁。已完成的 trajectory 不允许重复恢复。旧版没有 `task` 和 `steps` 字段的默认模板 trajectory 也可恢复；如果无法从旧消息中提取任务，可在命令末尾重新提供完全相同的任务文本。

## 配置

默认配置位于 `repo_agent/config/default.yaml`，分为四个顶层部分：

```yaml
agent:
  agent_class: default
  max_steps: 5
  system_template: "... {{ task }} ..."
  instance_template: "... {{ task }} ..."
environment:
  environment_class: local
  cwd: .
  timeout: 30.0
  max_output_size: 100000
model:
  model_class: openrouter
  model_name: null
  observation_template: "... {{ output.output }} ..."
run:
  output_path: null
```

可以指定 YAML 文件，也可以使用嵌套 override：

```bash
repo-agent --config ./agent.yaml \
  --override environment.timeout=10 \
  --override model.model_name='some/model' \
  --override agent.max_steps=8 \
  "Inspect the project"
```

YAML 中的模板使用 Jinja2 `StrictUndefined`。Agent 模板可以使用 `task`、`max_steps`、`cwd` 和 `model_name`；模型 observation 模板可以使用 `output`。

## Python bindings

```python
from repo_agent.agents import get_agent
from repo_agent.environments import get_environment
from repo_agent.models import get_model

model = get_model({"model_class": "mock"})
environment = get_environment({"cwd": "."})
agent = get_agent(model, environment, {"max_steps": 5})
result = agent.run("Inspect the project")
print(result.status, result.answer)
agent.save("trajectory.json")
```

顶层 `repo_agent` 导出 `Agent`、`Model` 和 `Environment` Protocol，具体实现可通过 `agents`、`environments`、`models` 的 shortcut 或完整导入路径工厂选择。

## Trajectory

`DefaultAgent.messages` 保存线性消息轨迹，顺序为 system、user、assistant 和 observation。`agent.save(path)` 输出 JSON，包含版本、任务、状态、答案、结构化步骤、完整消息和组件配置。组件配置中的 API key、token、secret 和 password 字段会递归脱敏。

## 安全边界

LocalEnvironment 固定工作目录，限制执行时间和输出大小，并拦截少量明显危险命令，例如系统级 `rm` 和 `git reset --hard`。这些是应用层护栏，不是操作系统沙箱；它仍使用本地 shell 执行命令。需要强隔离时，应在容器、namespace 或独立沙箱中运行。

## 开发与测试

```bash
python -m pytest
python -m compileall -q src tests
```

测试覆盖消息轨迹、证据策略、提交解析、安全执行、配置工厂、provider 无网络契约、trajectory 保存和 Mock CLI。

架构组织方式受到 mini-swe-agent 的启发，感谢其对轻量 SWE agent 组件化设计的参考价值。

发布历史见 [CHANGELOG.md](CHANGELOG.md)，发布前检查见 [RELEASING.md](RELEASING.md)，贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全边界与漏洞报告方式见 [SECURITY.md](SECURITY.md)，第三方声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
