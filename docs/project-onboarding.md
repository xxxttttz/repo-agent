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
