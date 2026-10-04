# Repo Agent

> 当前版本：v0.1.0（Alpha）。核心 coding-agent 流程已经可用，执行本地命令时仍应使用隔离环境并检查 trajectory。

Repo Agent 是一个精简的本地 coding agent：模型逐轮提出 shell 命令或结构化文件编辑动作，环境执行并返回 observation，只有在模型使用独立提交命令且现有证据策略通过后，任务才会被接受为完成。

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

调查任务会提示模型尽早读取完整实现与错误处理分支。最近 6 步内若有 3 次成功读取返回相同的非空内容，Agent 会加入恢复提示，建议更换调查目标或读取完整源码；这只是提示，不会阻断命令，也不会把重复运行测试判作读取停滞。

并且至少执行过一个成功的非提交命令。任务中提到的目标文件还需要匹配成功的独立读取命令；复合命令、重定向和仅包含文件名子串的命令不再作为文件读取证据。这仍是命令层面的启发式检查，不代表任务已经正确实现。提交命令输出 marker 后的文本会成为最终答案；如果没有后续文本，则使用 assistant message 的内容作为答案。

### 结构化文件编辑

模型可在 `command` 中直接使用编辑对象，例如：

```json
{"content":"转换超时单位","command":{"type":"edit","mode":"replace","path":"client.py","old_text":"timeout_seconds = timeout_ms","new_text":"timeout_seconds = timeout_ms / 1000"}}
```

`replace` 要求原文唯一匹配；`create` 要求文件不存在、`old_text` 为空。匹配失败、歧义或 Python 语法错误会报错，不会覆盖目标文件。成功后返回 diff 和 SHA-256。Local 与 Docker 都支持，动作可保存和恢复；默认提示优先使用编辑对象进行文件修改。详见 [编辑工具说明](docs/editing.md)。

### 提交前验证

可通过重复的 `--verify` 参数或 YAML 的 `agent.verification_commands` 设置必过检查：

```bash
repo-agent --workspace ./my-project --max-steps 20 \
  --verify 'git diff --check' \
  --verify 'python -m pytest -q' \
  '修复登录超时并补充回归测试'
```

每次提交通过基本证据检查后，执行器都会在相同 workspace 和相同 local/Docker 环境中按顺序重新运行验证命令，沿用命令超时、输出限制和环境变量策略。检查失败、被拒绝或超时会拒绝提交，把诊断送回模型；后续提交从第一个检查重新运行。验证不占模型步骤数，结果单独保存在 trajectory 的 `verifications` 中，并通过 `submission_step` 关联提交。`--resume` 沿用已保存的检查配置，但不会复用旧的通过结果。显式 `--verify` 会替换原有检查列表。

HTTP `POST /tasks` 同样接受 `verification_commands` 字符串数组。未配置时默认没有自动测试；检查命令由调用者选择，退出码为零只是所选检查通过，不保证全部需求正确，也不能防止模型修改测试。验证命令可能写入文件，应使用实际项目中可信、适合当前环境的命令。

预算不少于 4 步时，剩余 3 步和 1 步的模型调用前会提醒收尾，并提示当前基本文件证据缺口；恢复任务按本次追加预算计算。提示不自动延长预算或强制提交，耗尽仍返回 `max_steps`。未配置验证时，“最后一次修改后再测试”只是模型指令，并非强制完成条件。

模型被要求在每次提交（包括被拒后重新提交）给出完整的变化或调查结论、实际观察到的验证证据与未验证事项。执行器另外生成 `handoff` 回执，随 `AgentResult`、trajectory 和 HTTP 结果返回，CLI 单独显示：检查未配置、未运行、通过或未获接受不会混为一谈；已成功的结构化编辑动作也会列出，但不冒充最终文件 diff。模型回答保持原样，回执不为其全部文字背书，详见 [交付与验证回执](docs/handoff.md)。

### 保护不应修改的文件

使用重复的 `--protect PATH`、YAML `agent.protected_paths` 或 HTTP `protected_paths` 数组明确指定受保护文件，例如：

```bash
repo-agent --workspace ./my-project --max-steps 20 \
  --protect README.md --protect pyproject.toml \
  --verify 'python -m pytest -q' '修复缺陷，保持文档和依赖配置不变'
```

路径必须是 workspace 内的相对文件路径，不支持目录、glob 或 `..`。任务开始时记录内容指纹、文件类型与权限，提交时在验证命令前后各检查一次；删除、创建原先不存在的受保护文件或修改内容/权限都会拒绝提交。指纹写入 trajectory 的 `protected_files`；恢复时使用原始记录，不会把外部改动重新认定为基线。旧轨迹若没有所选文件的原始指纹，会拒绝恢复。显式 `--protect` 替换配置中的文件列表。

此检查只控制是否接受完成，不拦截 shell 写入、不自动回滚，也不检测修改后又恢复的临时行为。符号链接只比较链接目标，不读取链接指向的文件内容。保护文件由调用者显式选择，不会根据自然语言自动推断。

Git workspace 的 HTTP 任务默认在独立 worktree 中执行；成功任务沿用自动提交与合并流程。耗尽步数、取消或异常退出的任务保留 worktree 和分支，`worktree_cleaned` 为 `false`，可从任务响应的 `worktree_path` 找回未提交修改。保留的 worktree 需要人工检查和清理，目前不自动回收，也不自动续跑 HTTP 任务。

### 本地源码检索

`repo_agent.retrieval` 提供不增加第三方依赖的源码索引和 BM25 词法检索。Python
文件按顶层函数和类分块，其他支持的文本文件按行分块：

```python
from repo_agent.retrieval import BM25Retriever, build_index, format_results

chunks = build_index("./my-project")
results = BM25Retriever(chunks).search("completion policy", top_k=5)
print(format_results(results))
```

检索结果包含相对路径、符号名和行号，可作为 Agent 的候选上下文。CLI `search`
只输出候选上下文；HTTP 任务服务会自动用任务文本检索，并把 top-k 结果作为只读上下文
注入 Agent。原始任务文本仍独立用于 evidence policy 校验。

也可以直接通过 CLI 检索，不需要配置模型或 API key：

```bash
repo-agent search "completion policy" --workspace . --top-k 5
./repo-agent search "代码检索" --workspace ./my-project
```

### Redis 增量索引缓存

设置 `REDIS_URL` 后，索引器会用“分块器版本 + 文件扩展名 + 文件内容 SHA-256”作为
Redis key。每次仍会扫描可索引文件并计算哈希，以识别变化，但内容未变的文件会直接
反序列化已有 chunks，不再重复 AST 解析或按行分块。缓存默认保留 7 天；Redis 读取、
写入或连接失败会记 warning 并退化为正常构建，不会让任务失败。

```bash
REDIS_URL=redis://localhost:6379/0 \
  repo-agent search "completion policy" --workspace .
```

也可以用 `--redis-url` 显式传入。缓存值不保存绝对路径，相同内容可跨 workspace
复用，同时避免把宿主机路径写进 Redis。

## HTTP 服务与 Docker

服务提供异步任务接口：`POST /tasks` 返回 `202`、任务 id 和 session id，`GET /tasks/{id}` 返回
`queued`、`running`、Agent 的终态、trajectory，以及本次索引的
`files/chunks/cache_hits/cache_misses` 指标。

任务还支持取消和 SSE 状态订阅：

```bash
curl -N http://localhost:8000/tasks/TASK_ID/events
curl -X POST http://localhost:8000/tasks/TASK_ID/cancel
```

SSE 会在任务快照变化时发送 `task` 事件，并在进入 `completed`、`max_steps`、`error`
或 `cancelled` 后结束连接。取消是协作式的：排队任务会立即取消；运行中任务会在当前
模型请求或 shell 命令结束后的步骤边界停止。

配置 Redis 时，任务通过 Redis Streams consumer group 投递。worker 只有在任务进入终态
后才会 ACK；进程异常留下的 pending 消息默认在 30 秒后由其他 worker 重新认领。执行中
任务采用 at-least-once 语义，因此 workspace 中的修改操作最好保持幂等，并在更高安全
要求下为每个任务使用独立 worktree。认领等待时间可通过
`REPO_AGENT_QUEUE_RECLAIM_MS` 调整。

```bash
docker compose up --build

curl -i -X POST http://localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{"task":"Explain the retrieval cache","workspace":"project","provider":"mock"}'

curl http://localhost:8000/tasks/TASK_ID
```

### Redis 会话记忆

相关任务可以共享一个 session。会话按 workspace 隔离，默认在 Redis 中保留最近 8 个
已完成任务的“任务文本 + 最终答案”，每次访问后 TTL 刷新为 24 小时。历史作为上下文
注入后续 Agent，但当前任务始终拥有更高优先级。

```bash
# 可选：先显式创建 session
curl -X POST http://localhost:8000/sessions \
  -H 'content-type: application/json' \
  -d '{"workspace":"project"}'

# 后续任务复用返回的 session_id
curl -X POST http://localhost:8000/tasks \
  -H 'content-type: application/json' \
  -d '{"task":"记住我们使用 Redis","workspace":"project","session_id":"SESSION_ID"}'

curl 'http://localhost:8000/sessions/SESSION_ID?workspace=project'
```

不传 `session_id` 时服务会自动生成，并在 `POST /tasks` 响应中返回；要延续记忆，下一
次请求必须带回同一个 id。可通过 `REPO_AGENT_SESSION_TTL` 和
`REPO_AGENT_SESSION_MAX_TURNS` 调整过期秒数及最大轮数。未配置 Redis 时会退化为
进程内会话记忆，重启即丢失。

Compose 会启动 API 与持久化 Redis，并把当前仓库挂载到容器内的
`/workspace/project`；API 容器默认沿用宿主机的 `UID/GID`，因此 Agent 可以修改挂载
仓库且不会生成 root-owned 文件。默认使用 `mock`，需要真实模型时可设置
`REPO_AGENT_PROVIDER`、`REPO_AGENT_MODEL` 及相应 API key 后重启。

不使用 Docker 时：

```bash
python -m pip install -e '.[service]'
REPO_AGENT_WORKSPACE_ROOT=/path/to/workspaces \
REDIS_URL=redis://localhost:6379/0 repo-agent-api
```

### 命令执行隔离

HTTP 服务使用本地执行器时默认不会把服务进程的环境变量传给 Agent 命令，只提供安全的
`PATH`、`HOME`、`TMPDIR` 和 locale。确实需要的变量可以显式加入逗号分隔的
`REPO_AGENT_ENV_ALLOWLIST`；模型 provider 的 API key 通常不需要加入。

也可以让每条 Agent 命令在一次性 Docker 容器中运行：

```bash
REPO_AGENT_ENVIRONMENT=docker \
REPO_AGENT_DOCKER_IMAGE=python:3.13-slim \
repo-agent-api
```

Docker 执行器默认使用 `--network none`、只读根文件系统、`no-new-privileges`、删除全部
capabilities，并限制为 1 CPU、1 GiB 内存和 256 个 PID。目标 workspace 单独读写挂载到
`/workspace`，临时目录使用受限 tmpfs。默认 `--pull=never`，镜像需要提前存在。
可以使用以下环境变量调整服务端策略：

- `REPO_AGENT_DOCKER_NETWORK`
- `REPO_AGENT_DOCKER_MEMORY`
- `REPO_AGENT_DOCKER_CPUS`
- `REPO_AGENT_DOCKER_PIDS_LIMIT`
- `REPO_AGENT_DOCKER_READ_ONLY`
- `REPO_AGENT_DOCKER_PULL`

Docker 模式要求 `repo-agent-api` 进程能够调用 Docker CLI。直接在宿主机启动 API 时最
简单；若 API 自身运行在 Compose 容器中，需要自行提供 Docker CLI、daemon socket 和
正确的宿主机 workspace 映射。挂载 Docker socket 等同于向 API 容器授予很高的宿主机
权限，生产环境更适合使用独立的远程 sandbox worker。

宿主机使用本地代理时，也可以只让 Redis 运行在 Compose 中，并在宿主机启动 API：

```bash
docker compose stop api
docker compose up -d redis
set -a && . ./.env && set +a
REPO_AGENT_WORKSPACE_ROOT="$PWD" \
REDIS_URL=redis://127.0.0.1:6379/0 \
HTTP_PROXY=http://127.0.0.1:10808 \
HTTPS_PROXY=http://127.0.0.1:10808 \
repo-agent-api
```

此模式下仍完整经过 FastAPI 和 Redis，只是 API 进程直接复用宿主机代理。

服务拒绝访问 `REPO_AGENT_WORKSPACE_ROOT` 之外的目录。配置 Redis 后，任务快照默认保留
7 天，因此 API 重启后仍可查询已知 task id；可通过 `REPO_AGENT_TASK_TTL` 调整保留时间。
Redis Streams 会恢复未确认的 `queued` 或 `running` 任务，也允许多个 API 实例共享消费；
同一 workspace 的执行由 Redis 租约锁串行化。锁会自动续约，只有持有匹配 token 的
worker 才能释放；如果执行期间丢失租约，任务会停止并进入 `error`，避免在失去所有权后
继续修改仓库。租约时长可通过 `REPO_AGENT_WORKSPACE_LOCK_LEASE_MS` 调整，默认 30 秒。
未配置 Redis 时使用进程内 workspace 锁。Agent 仍会在挂载的 workspace 执行命令，
因此生产环境应使用专用容器或更强的沙箱，并配置认证和网络边界。

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

新增可重复运行的编码任务评测，覆盖单文件修复、跨文件修改、功能新增和只读调查：

```bash
repo-agent-eval --list
repo-agent-eval --provider mock --max-steps 5 --output /tmp/repo-agent-eval
```

每轮使用全新 fixture workspace，独立检查实现行为、修改范围、公开测试和回答事实，并保存轨迹与 JSON 报告。Mock 不会修复任务，预期评分失败、退出码为 1；真实模型需显式指定 provider。当前只有四个手工任务，适合回归检查，不能代表真实仓库成功率。指标含通过率、假完成率、步数、耗时和无关修改数。详见 [评测说明](docs/evaluation.md)。

评测同样支持重复的 `--verify '检查命令'`，在提交时重跑调用者指定的检查并记录配置；独立验收仍在模型停止后运行，不向模型泄露验收源码或结果。

```bash
python -m pytest
python -m compileall -q src tests
```

测试覆盖消息轨迹、证据策略、提交解析、安全执行、配置工厂、provider 无网络契约、trajectory 保存和 Mock CLI。

架构组织方式受到 mini-swe-agent 的启发，感谢其对轻量 SWE agent 组件化设计的参考价值。

发布历史见 [CHANGELOG.md](CHANGELOG.md)，发布前检查见 [RELEASING.md](RELEASING.md)，贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全边界与漏洞报告方式见 [SECURITY.md](SECURITY.md)，第三方声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
