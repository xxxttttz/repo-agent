# Python CI 修复工单台（Alpha）

定位：给受信任的内部团队提供一个“小范围 Python 测试失败修复”的工单入口。用户提交目标和失败日志；管理员固定仓库、模型和验收策略；模型产出可审查的补丁，由人决定是否合入。

当前不接收任意仓库 URL，不自动安装依赖，不调用 GitHub/GitLab 创建 PR，也不代替代码审查。模型修复效果取决于所配置的模型和项目；自动化闭环测试使用脚本模型，不代表真实模型成功率。

## 启动

先准备一个专用、受信任的 Git 仓库，例如 `/path/to/workspaces/project`。仓库须有提交和已检出的分支，源工作区必须干净；依赖、Git 和 Python 提前准备好。执行 worktree 不会复制未提交文件、被忽略的虚拟环境或本地构建产物。

从本项目源码目录安装服务依赖：

```bash
python -m pip install -e '.[service]'
```

以 [模板示例](../config/repair-profiles.example.yaml) 为起点，通过编辑器创建自己的配置（建议存放在目标仓库之外）。至少修改 `workspace`、`provider`、`model` 和测试命令；示例 `mock` 只能验证服务接线，不能修复真实缺陷。

```bash
export REPO_AGENT_WORKSPACE_ROOT=/path/to/workspaces
export REPO_AGENT_REPAIR_PROFILES=/path/to/repair-profiles.yaml
export REPO_AGENT_WORKTREE_ROOT=/path/to/persistent-worktrees
export REPO_AGENT_API_TOKEN='replace-with-a-long-random-token'
# 按模板 provider 配置对应模型凭据，例如 OPENROUTER_API_KEY。
# 可选：export REDIS_URL=redis://127.0.0.1:6379/0
repo-agent-api
```

访问 `http://127.0.0.1:8000/`，填写访问令牌，加载模板，然后提交修复目标和失败日志。首页历史列表可按状态、类型和模板仓库筛选，选择“待人工审批”即可查看候选队列。点击工单摘要后展示失败复现、验收、范围检查、执行器回执和完整候选 diff；也可以粘贴 task ID 找回已知工单。

上述令牌是占位符，实际部署须使用随机令牌。令牌仅保留在当前页面输入框，不写入浏览器持久存储。首页和健康检查公开，其余接口在配置令牌后需要 Bearer 认证。没有配置令牌则没有认证，请不要公开服务。默认仅监听本机；公网或团队远程使用需要 TLS、访问控制和专用执行环境。

## 管理员模板

```yaml
profiles:
  - id: python-unittest
    title: Python 单元测试修复
    workspace: project
    provider: openrouter
    model: your-provider-model-id
    reproduce_commands:
      - python3 -B -m unittest discover -s tests
    verification_commands:
      - python3 -B -m unittest discover -s tests
      - git diff --check
    allowed_paths:
      - src/*.py
      - tests/test_regression_*.py
    protected_paths:
      - README.md
      - pyproject.toml
      - tests/test_existing_behavior.py
    max_steps: 20
    max_changed_files: 5
    command_timeout: 30
    deadline_seconds: 600
```

`workspace` 必须位于服务 workspace root 内，并指向 Git 仓库根目录。测试命令应读取当前候选工作区的代码；不要让预先安装的 editable package 或绝对路径测试实际上验证源仓库。`src` 布局通常需要按项目配置 `PYTHONPATH=src` 或其他正确的导入方式。模型运行的 local 命令默认不继承服务环境变量，必要变量应明确配置 `REPO_AGENT_ENV_ALLOWLIST`；不应把模型 API key 放入命令环境。

范围模式使用 Python `fnmatchcase`，区分大小写，`*` 可以匹配 `/`：`src/*.py` 也允许 `src/package/module.py`。范围检查包含新增、修改、删除和重命名后的新旧路径，最多允许 100 个模式。`protected_paths` 是相对文件名，不接受目录、glob、绝对路径或 `..`，可用于保护原有测试、依赖文件和文档。保护文件比较最终内容、类型和权限，不检测临时修改后恢复。

复现和验收各最多 20 条命令。预算上限为模型动作 100 步、修改文件 100 个、每命令 300 秒、任务 3600 秒；实际可设置更小值。任务时间从开始执行阶段计算，不含排队和创建 worktree，且在动作边界检查，不是强制中断模型请求的硬截止；审批重新验收另计时间。`REPO_AGENT_MAX_PENDING` 默认 100，是单进程待执行/执行中任务限额，不是分布式全局配额或费用限额。

模板是可信管理员配置，不能交给工单请求者编辑。命令应是只读的测试、静态检查和 diff 检查；它们本身同样可以执行任意代码。模板在服务启动时加载；每份工单保存配置快照，之后更改模板不会改写该工单的验收策略。`REPO_AGENT_ALLOW_GENERAL_TASKS=true` 会重新开放通用任务接口，仅在管理员明确需要时开启。

## 执行与审批

```text
工单 → 独立 worktree → 复现失败 → 模型修复 → 独立验收/保护/范围检查
                                              ↓
                                      候选 commit + 完整 diff
                                              ↓
                                        awaiting_review
                                         ↙          ↘
                                  拒绝并保留     人工批准 → 重新验收
                                                       → 校验源版本
                                                       → 精确 commit 快进合入
```

| 状态 | 含义 |
| --- | --- |
| `queued` / `running` | 排队或执行中，可协作式取消 |
| `not_reproduced` | 配置的复现命令全部通过，没有调用模型 |
| `awaiting_review` | 执行已结束，候选未合入；需人工决定 |
| `approved` | 重新检查后已将候选 commit 合入源仓库 |
| `rejected` | 人工拒绝，源仓库不变，候选保留 |
| `error` / `max_steps` / `cancelled` | 未获接受，保留可用的工作区和轨迹 |

超时、命令不可执行或非法提交 marker 不算成功复现。其他普通非零退出码视为复现失败，因此 Python 导入错误、依赖缺失也可能被当作待修复失败；应由管理员确保测试环境正确，审查基线日志。检查通过只证明所选命令通过，不证明所有需求正确。

候选包含原始 base commit、候选 commit、完整二进制兼容 diff、修改路径及 diff 的 SHA-256。大于 1 MB 的 diff 会拒绝生成可审批候选，不会截断后让人批准。审批必须提交精确 commit 和 hash；源分支、源 HEAD 或候选分支/工作区变化、源目录不干净、重新验收失败都会拒绝合入，通常返回 `409`。验收命令若写入未忽略的候选文件，也会使审批失败。源版本变化后应重跑工单，不自动 rebase 或合并未审查的新补丁。

审批使用仓库租约锁，重新核验后执行 `git merge --ff-only COMMIT`，失败不做破坏性 reset。同一工单重复相同决定在正常流程中幂等，不能批准已拒绝工单。只在批准后将修复结果写入会话记忆；模型的 `result.status=completed` 或 handoff 表示 Agent 层接受，工单顶层 `status` 才表示最终流程状态，二者不能混用。

## API

所有示例假设 `REPO_AGENT_API_TOKEN` 已设置。创建请求只能包含 `profile`、`task` 和 `failure_log`，不能传入自选仓库、命令或模型：

```bash
curl -X POST http://127.0.0.1:8000/repairs \
  -H "Authorization: Bearer $REPO_AGENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"profile":"python-unittest","task":"修复边界错误并补回归测试","failure_log":"AssertionError: expected 6, got 3"}'

curl http://127.0.0.1:8000/tasks/TASK_ID \
  -H "Authorization: Bearer $REPO_AGENT_API_TOKEN"

curl http://127.0.0.1:8000/tasks/TASK_ID/candidate \
  -H "Authorization: Bearer $REPO_AGENT_API_TOKEN"

curl -X POST http://127.0.0.1:8000/tasks/TASK_ID/approve \
  -H "Authorization: Bearer $REPO_AGENT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"commit":"EXACT_CANDIDATE_COMMIT","diff_sha256":"EXACT_DIFF_SHA256","reason":"已审查补丁和回归测试"}'
```

拒绝使用相同请求体调用 `/tasks/TASK_ID/reject`；取消执行调用 `POST /tasks/TASK_ID/cancel`。SSE `/tasks/TASK_ID/events` 在 `awaiting_review` 等执行终态结束，审批之后重新查询任务。

普通 `POST /tasks` 的 Git 任务现在默认 `delivery_mode: "review"`；显式 `auto_merge` 才使用旧的自动合并行为。没有代码变化的普通调查任务可直接 `completed`，非 Git 或禁用 worktree 的普通任务仍直接执行，不提供候选审批隔离。修复入口不接受这些降级模式。

## 候选补丁与验证摘要导出

工单进入 `awaiting_review` 后，详情页显示“下载 patch”和“下载验证摘要”。批准或拒绝后的候选也可导出存档；未产生候选、仍在运行或异常状态的任务不能导出。两份文件不触发模型调用、验证命令、审批、合并、推送或 PR 创建。

API 需要同时传入当前候选的 `commit` 和 `diff_sha256`，与审批绑定相同身份：

```bash
curl --get 'http://127.0.0.1:8000/tasks/TASK_ID/candidate.patch' \
  -H "Authorization: Bearer $REPO_AGENT_API_TOKEN" \
  --data-urlencode 'commit=EXACT_CANDIDATE_COMMIT' \
  --data-urlencode 'diff_sha256=EXACT_DIFF_SHA256' \
  --output candidate.patch

curl --get 'http://127.0.0.1:8000/tasks/TASK_ID/candidate-report.json' \
  -H "Authorization: Bearer $REPO_AGENT_API_TOKEN" \
  --data-urlencode 'commit=EXACT_CANDIDATE_COMMIT' \
  --data-urlencode 'diff_sha256=EXACT_DIFF_SHA256' \
  --output candidate-review.json

sha256sum candidate.patch
```

`sha256sum` 结果应与报告 `candidate.diff_sha256` 相同。参数缺失/格式不对返回 `422`，工单不存在返回 `404`，身份不匹配、候选未就绪或保存的字节校验失败返回 `409`。实际使用时请将占位值替换成 `GET /tasks/TASK_ID/candidate` 返回的精确值，避免把错误 JSON 当成 patch；curl 可加 `--fail-with-body` 检查 HTTP 错误。下载前确认目标文件是新文件，以上 `--output` 会覆盖同名文件。

patch 是已保存 diff 的原始 UTF-8 字节，包含 Git 的二进制兼容补丁，不追加换行、不截断、不脱敏。服务重新核对这些字节的 SHA-256，并限制在 1 MB 内；不从当前工作区重新生成 diff，即使工作区发生变化，导出的仍是原候选。请审查代码、文件名及可能的敏感内容，确认后再分享。

验证摘要是 `schema_version: 1` 的 JSON，包含工单元数据、候选 base/commit/hash/修改路径、控制器复现/最终/审批阶段的检查状态与退出码、范围检查状态，以及已保存的审查决定和时间。每个检查只有序号，没有命令或原始输出；对应命令和详细诊断需回到服务查看。它不包含任务正文、失败日志、错误文本、模型回答、trajectory、模板配置、绝对工作区位置或审查理由。

`not_recorded` 表示未保存该阶段证据，不等于通过或失败；`not_configured` 表示明确未配置检查。普通通用任务的 Agent 内部检查不投影到控制器最终检查里，所以 `final=not_recorded` 不代表它从未运行测试。范围证据的路径与候选不一致时标为 `inconsistent`，不冒充已通过当前候选的范围检查。

报告只是已保存控制器证据的白名单投影，不是可验证签名，也不保证当前源仓库/候选工作区仍可合入或代码正确。导出不重新检查工作区、执行新验收或更新工单；Git hooks 和后续外部改动可能影响记录的适用性。批准时仍须走原来的重新验收与精确 commit 快进流程。若在服务外使用 patch，应在独立、干净且版本正确的仓库中先执行 `git apply --check`、审查并重新测试，不自动沿用历史批准状态。

附件文件名来自校验后的 commit 前 12 位，不使用任务正文或请求路径。响应带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`X-Repo-Agent-Commit` 和 `X-Repo-Agent-Diff-SHA256`。页面通过带 Bearer 请求头的 fetch 获取附件，不把令牌放在下载 URL 中；在浏览器提供 Web Crypto 时额外校验 patch 字节，发现身份/hash 不匹配或已切换工单就不会触发该旧下载。Web Crypto 不可用时仍有服务端字节校验，可用上述命令独立复核。

## 历史列表与筛选

```bash
curl 'http://127.0.0.1:8000/tasks?kind=ci_repair&status=awaiting_review&limit=20' \
  -H "Authorization: Bearer $REPO_AGENT_API_TOKEN"
```

返回 `tasks`（摘要数组）、`next_cursor`（下一页游标或 `null`）、`degraded`（是否仅来自本机缓存）和 `history_limit`。摘要包含 id、截断为最多 240 个字符加省略号的任务文本、状态、类型、模板 id、创建/执行时间、源仓库和交付模式，不包含失败日志、模型消息、轨迹、验收命令或 diff；完整详情需要另行查询。

支持 `status`、`kind=ci_repair|task`、`workspace` 和 `limit`（1–100，默认 20）。`workspace` 的相对路径按服务根目录解释，越界会拒绝；未指定时也只列出服务根目录范围内的记录。这是同一个共享操作员权限范围，不是按用户隔离的仓库 ACL。所有列表请求遵循既有令牌认证，并返回 `Cache-Control: no-store`。

按“创建时间 + id”倒序，用 `next_cursor` 继续相同筛选；新增工单不会改变旧游标对应的边界。切换筛选时清空游标、重新查询。列表不是状态快照，工单状态可以在翻页期间变化；审批、取消、执行状态变化时页面会刷新列表，其他操作员的变化需手动刷新。

历史索引最多保留最近 10,000 项创建记录；超过窗口的已知 task ID 仍按任务存储规则查询，不自动删除对应轨迹或 worktree。Redis 新版本索引只覆盖本功能上线后创建的记录，不自动扫描回填旧任务；升级前记录仍可用已知 ID 查找。较早版本缺少的摘要字段可能为空。

Redis 查询按批次读取摘要字段，不拉取完整工单。单次最多扫描 1,000 个索引项，稀少筛选可能返回空 `tasks` 但仍有 `next_cursor`，需要继续翻页。过期 Redis hash 会从索引中惰性移除，不用本机缓存冒充存活记录。Redis 读取异常时退化为本机缓存，并置 `degraded=true`；缓存只涵盖该进程曾创建或加载的记录，不能据此断言没有待审批任务。未配置 Redis 时正常使用进程内列表，重启即丢失。

## 持久化、部署与安全边界

- Redis 保存任务快照、队列和会话；未配置 Redis 时使用进程内存，服务重启丢失工单记录。Redis 任务默认保留 7 天（`REPO_AGENT_TASK_TTL`），过期记录不会自动删除 Git worktree。
- 审查模式所有 worktree/分支都保留，包括通过、拒绝、未复现和失败任务。需要人工检查并按精确 task 路径清理；没有自动回收按钮或续跑功能。Redis 持久化并不等于 worktree 持久化，必须同时保留源仓库、管理目录和稳定的绝对路径。
- Compose 默认挂载本项目为 `/workspace/project`，适合服务接线演示，不是通用项目模板。实际目标仓库和模板应另行挂载，并按配置指定路径。默认 worktree 在容器 `/tmp`，容器重建会丢失：需将 `REPO_AGENT_WORKTREE_ROOT` 指向持久化挂载，并确保 API 的 UID/GID 可写。不要仅依赖 Redis volume。
- Docker 执行器只挂载候选工作区，Git worktree 的 `.git` 引用可能指向容器不可见的源仓库元数据；因此示例里的 `git diff --check` 不能原样假设在这种环境可用。应针对镜像/挂载验证命令，或移除容器内 Git 检查，使用控制器的 Git diff 加其他验收。当前真实 Docker 修复闭环未验证；自动化只覆盖执行器协议。
- 本地执行器、范围护栏和保护文件不是操作系统沙箱。shell、项目测试、Git hooks、日志提示注入都可能越权或访问宿主机数据；Git worktree 共享 Git 元数据，模型也可能自行调用 Git。仅处理受信任仓库，在专用隔离 worker 中运行，不应开放为任意用户提交任意代码的公共服务。
- 共享令牌没有角色划分：持有令牌者既可创建工单，也可审批和读取日志。日志不自动脱敏，提交前应移除凭据；代码、日志和历史可能发给模型供应商。尚无用户审计、费用统计、token 预算、Webhook 或 PR 集成。
- Git 合入和任务快照更新不是跨系统事务；若进程恰在合入后、记录更新前崩溃，需要人工核对源 HEAD 和候选 commit。外部 Git 写入不受服务租约管理，也没有自动冲突恢复。

## 验证范围

`tests/test_repairs.py` 使用真实临时 Git 仓库、真实 unittest 和脚本模型，覆盖结构化编辑、新增回归测试、复现/验收、审批/拒绝、候选 hash、版本变化、保护文件、范围/预算、重新验收写入、取消/时间边界、认证以及 HTTP 审批。它验证控制流程，不衡量真实模型修复率，也不验证浏览器的全部交互。

历史相关测试覆盖内存与模拟 Redis 的分页、并发插入顺序、过滤、过期索引、重启、缓存降级、索引窗口、分页扫描上限，以及 API 的认证、字段裁剪和审批前后列表状态。当前没有运行真实 Redis 历史索引集成测试或完整浏览器回归。

导出测试覆盖精确字节/UTF-8/hash、候选身份绑定、附件头、权限、异常和大小上限、报告白名单、未记录/未配置的区分、工作区外部变化后的原候选存档，以及在真实临时 Git 源仓库中 `git apply --check`。导出不会改变任务状态、会话记忆或当前工作区。

另外可使用 [修复闭环评测 CLI](repair-evaluation.md) 重复运行内置失败仓库：每次通过生产控制器生成候选，并在模型上下文之外运行独立行为验收、检查新增测试和源仓库是否改变。这个入口不自动批准候选，也不调用远程 Git 服务。
