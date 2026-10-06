# 项目接入检查：还没有 CI 失败日志时

没有真实失败日志也可以先检查项目是否具备接入条件，不需要人为制造错误。
`repo-agent-preflight` 不加载模型、不创建修复工单、不安装依赖、不生成补丁，
也不会自动提交、合并、推送或清理仓库。

## 先检查，再明确选择测试

安装当前版本后可以使用新入口；源码开发环境可直接使用
`python -m repo_agent.preflight`，不需要重新安装 console script。

默认只读取 Git 和项目配置文件的存在情况，不读取配置内容、不猜测测试命令：

```bash
repo-agent-preflight --workspace /absolute/path/to/project
```

指定的是仓库根目录。已有提交、非 detached HEAD、无非忽略的未提交改动时，
报告 `not_configured`：仓库初步检查完成，但尚未执行测试。
仓库脏、detached HEAD 或指定子目录时阻止执行，不会自动 stash、提交或回滚。

管理员确认项目和命令可信、依赖已准备好后，显式指定检查：

```bash
repo-agent-preflight --workspace /absolute/path/to/project \
  --check '/absolute/path/to/venv/bin/python -m pytest -q' \
  --check 'git diff --check' \
  --timeout 60
```

检查在**指定的本地源目录**运行，而不是自动创建 worktree 或 OS 沙箱。
测试本身可能写文件或运行任意代码；不能用于未经审查的不可信仓库。
需要保护原目录时，先准备独立副本或受控执行环境，再将其作为 workspace。
最多 20 条命令，每条超时最多 300 秒；没有独立的总体 deadline。

检查默认不继承宿主机环境变量（包括模型密钥），PATH 使用系统默认值；
因此建议明确指定虚拟环境解释器的绝对路径。依赖业务环境变量、网络、数据库、
子模块或外部服务的测试，需要管理员先准备合适的执行环境；工具不会自动补齐。

## 报告与退出码

标准输出是 JSON，可保存在仓库外部；不要重定向到仓库内非忽略路径，
因为 shell 会在检查之前创建该文件，使仓库变脏。

| state | 含义 | 退出码 |
| --- | --- | --- |
| `not_reproduced` | 所选检查全部通过，没有可复现故障，不进入修复 | 0 |
| `checks_failed` | 命令执行完毕但有非零退出码，需要人工诊断 | 1 |
| `not_configured` | 尚未配置测试，只做了初步仓库检查 | 2 |
| `blocked` | 仓库状态不符合条件，未执行测试 | 2 |
| `error` | Git 查询异常、命令无法正常执行、超时或提交 marker 等 | 2 |
| `source_changed` | 执行前后 Git HEAD、分支或状态不一致，保留现场 | 2 |

检查失败不等于已经确认代码缺陷，例如缺失 Python 包、pytest 未发现测试
也可能表现为非零退出码。`not_reproduced` 仅代表所选命令通过，不代表测试一定
覆盖业务行为、没有跳过测试，或与远程 CI 一致。

报告保留基线状态及每条命令、执行状态、退出码、截断标记；默认不包含原始输出。
需要诊断失败时可加 `--include-logs`，但日志可能包含密码、Token 或业务数据，
分享前必须人工审查。命令文本和本地路径本身不脱敏，不能直接公开报告。

执行后的 Git 观察不一致会覆盖顶层成功状态，但原始 `baseline` 证据仍保留。
Git 观察不覆盖 ignored 文件、修改后又恢复的临时行为或仓库外写入，
不是文件系统完整性或执行安全的保证。工具不自动删除测试产生的文件。

## 下一步

如果检查通过，接入准备可以继续，但不要伪造失败日志启动修复。
后续出现实际失败时，确认失败命令与输出，管理员配置可信复现/验收命令、
允许修改范围及保护文件，再进入 [人工审批的修复工单流程](ci-repair.md)。
该入口不自动生成修复模板或业务契约测试。

## 网页接入检查

服务首页选择管理员模板后，可以点击“运行项目接入检查（不调用模型）”，
不需要填写修复目标或失败日志。当前复用 `REPO_AGENT_REPAIR_PROFILES` 的模板：
执行 `reproduce_commands`，不执行修复用的 `verification_commands` 或新增回归门禁。
仓库、命令、单条超时、总预算和执行环境均由管理员固定。

```bash
curl -i http://127.0.0.1:8000/project-checks \
  -H 'Authorization: Bearer YOUR_OPERATOR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"profile":"python-project"}'
```

该接口返回 `202` 和 task ID；`Location` 指向 `/tasks/{id}`。请求仅允许 `profile`，
不接受路径、命令、provider、预算、环境配置或 `include_logs` 覆盖。
认证与现有修复 API 一致，仍是共享操作员令牌，不是多租户权限系统。

网页流程和 CLI 的执行位置不同：CLI 在给定目录直接运行；网页要求启用 worktree，
先检查源仓库状态，再从当前已提交版本创建独立 worktree。命令沿用管理员配置的
local/Docker 执行器，不会绕过 Docker 配置回退到宿主机执行。
被忽略的 `.env`、`.venv` 和构建产物不会复制，应预先准备合适的解释器或镜像。

网页检查在整个执行期间持有源仓库锁，避免与本服务的建 worktree 或审批合入竞争。
取消、截止预算和租约失效按命令边界协作处理，不会即时中断所有命令；
租约丢失、释放失败或执行后 Git 查询失败都不能发布成功。
测试对执行 worktree 的非忽略改动、源仓库 HEAD/分支/状态变化会报告 `source_changed`，
保留现场，不自动清理、提交或回滚。worktree 仍共享 Git 元数据，不是安全沙箱。

报告在 `/tasks/{id}` 的 `preflight_report` 中，网页会直接显示其摘要和 JSON。
网页报告默认省略原始命令输出，但命令文本、本地路径和 Git 状态可能包含内部信息；
分享前需审查。检查失败时，需人工查看实际失败输出并排除环境问题，
不能仅凭 `checks_failed` 自动判定代码缺陷或伪造日志发起修复。

接入检查的任务类型为 `project_check`，可用现有取消接口和 SSE 跟踪。
历史列表支持 `kind=project_check` 和对应终态筛选，只显示摘要，不带报告或原始日志。
Redis 存储/队列、TTL、历史窗口和降级行为沿用现有任务机制；未配置 Redis 时仅存内存。
同样受当前进程 pending 上限限制；所有已创建 worktree 保留，不自动回收或提供 HTTP 续跑。

任何检查结果都不会自动加载模型、写入对话记忆、产生候选或启动修复。
要修复时仍需人工填写目标和真实失败日志，服务会在新 worktree 中重新复现，
不会复用旧报告作为修复完成或人工审批的凭据。

2026-10-05 本项目改动前的本地基线（commit `02eff43c4fbc74957d266f2ca547dfecf9c98017`）：
仓库干净，`.venv/bin/python -m pytest -q` 得到 **379 passed in 27.60s**。
这是本机实际测试结果，不是远程 GitHub Actions 成功记录，也不是修复成功率。

新增入口随后在该提交的独立本地 clone 上执行 pytest、Ruff 和 `git diff --check`，
得到 `not_reproduced`：**379 passed in 35.22s**，另外两项检查通过，执行前后的
HEAD、分支和 Git 状态一致，未调用模型。使用现有虚拟环境的绝对解释器路径，
副本不复制 `.env` 或 `.venv`；副本是本次试跑手动准备的，不是入口自动创建的。
本地完整报告保存在 `eval-results/onboarding-20261005/report.json`（被 Git 忽略，
不会随源码推送）。临时副本保留在 `/tmp/repo-agent-onboarding-wa_dw46c/project`，
不会自动清理；它是接入试跑，不是业务仓库修复试点。

包含新增入口的开发版本另外通过 **415 个测试（28.74s）**、Ruff、compileall
和 diff 检查；wheel/sdist 构建成功，并从 wheel 导入接入模块、核对 CLI 入口，
验证仅检查仓库时返回 `not_configured` / 退出码 2。未安装或发布构建产物。

2026-10-06 网页接入检查迭代：完整回归 **439 个测试**通过。
新 API 使用认证的进程内 ASGI 客户端，对上述干净版本的 managed worktree
实际执行 pytest、Ruff 和 diff 检查，三条命令均退出 0，报告 `not_reproduced`。
源仓库和执行 worktree 的 Git 观察均未变化，无模型结果或候选，未合入或推送。
报告保存在被 Git 忽略的 `eval-results/onboarding-web-20261006/report.json`，
该 worktree 保留在报告记录的精确 `/tmp` 路径。

控制台脚本通过语法检查和 Mock-DOM 冒烟检查（模板请求、文本渲染、终态按钮、
不自动修复和原有审查控制），API 认证/历史/SSE 由集成测试验证。
未验证真实浏览器布局、真实 Redis 多 worker 或实际 Docker 接入检查。
