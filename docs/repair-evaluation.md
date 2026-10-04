# CI 修复闭环评测

`repo-agent-repair-eval` 使用生产的 `TaskManager` 修复控制器，在内置、独立 Git 仓库中尝试修复 CI 失败，最后独立评分。它补充核心 `repo-agent-eval`：覆盖检索上下文、服务模型配置、worktree、复现、必过检查、范围/保护文件和候选交付，但不启动 HTTP 服务器，不评测 Redis、浏览器或 Docker 部署，也不自动批准补丁。

## 运行

安装服务依赖后使用新入口：

```bash
python -m pip install -e '.[service]'
repo-agent-repair-eval --list
repo-agent-repair-eval --provider mock --max-steps 2 --repeat 2
```

从源码运行的等价命令为：

```bash
PYTHONPATH=src python -m repo_agent.evaluation.repairs --list
```

Mock 不联网、不需要凭据，也不会修复任务，预期评分失败、退出码为 `1`。这是报告和失败路径的冒烟检查，不是模型效果评测。测试中的脚本参考模型验证成功路径，但 CLI 不提供知道答案的 solver。

显式选择在线 provider 和 model 可测试真实模型，会将内置任务、仓库内容和执行观察发给供应商，可能产生费用。调用前请确认数据和费用范围，并配置该 provider 现有环境凭据：

```bash
repo-agent-repair-eval --provider huggingface --model YOUR_MODEL_ID \
  --case pagination-boundary --max-steps 20 --timeout 30 --deadline 600 \
  --repeat 3 --output /path/to/new-evaluation-directory
```

所有在线评测必须显式设置 `--model`，避免隐藏默认值导致模型身份不可比较。输出目录必须是新目录，不覆盖已有结果；默认 `eval-results/repairs-<unique-id>`。每次重试创建新的模型实例、源 Git 仓库和 worktree，不复用前一次改动。不接受用户仓库路径或远程 URL，不推送代码、不创建 PR、不调用审批接口。

## 用例与评分

| 用例 | 可见 CI 失败 | 独立验收另外检查 |
| --- | --- | --- |
| `pagination-boundary` | 第一页丢失末项 | 多页边界、空输入、越界、非法参数 |
| `timeout-units` | 默认超时把毫秒当作秒 | 多种单位换算、配置接口保持不变、非法输入 |

两个用例从已有核心 fixture 派生，但增加了可复现的失败测试，使用独立 suite hash，不能直接比较两套评分。保护原有测试、CI 失败测试、README 和 `.gitignore`；仅允许实现文件和新增 `tests/test_regression_*.py`，最多改动 5 个文件。独立验收源码和结果不注入模型，只向它提供任务、公开 CI 日志和控制器验收。

成功必须同时满足：

1. 工单进入 `awaiting_review`，生产控制器已接受候选，但尚未合入。
2. 模型停止后，评测自己的行为验收通过。
3. 公开测试通过，数量至少 3 项（原有 2 项，加至少 1 项），候选包含新增回归测试文件。
4. 源仓库 HEAD、已检出的分支和 tracked/untracked 工作区状态保持不变。

“公开 CI 通过并可供审查”不等于“独立行为验收通过”。报告以 `review_ready` 表示前者，以 `passed` 表示全部评分条件通过；`false_candidate` 指已进入待审查状态却未通过独立评分的候选，不表示它被合入。回归测试数只是最低要求，不衡量覆盖质量。关键词式 `summary_coverage` 仅作诊断，不改变评分。

即使模型失败、预算耗尽或 provider 断连，也保留该次结果并计入运行总数。对未完成结果仍执行独立检查，但不会将“代码正确却没有形成候选”记为成功。

## 产物

```text
eval-results/repairs-<id>/
├── summary.json
└── <case>-<attempt>/
    ├── source/           原始 Git 仓库，应保持不变
    ├── worktrees/        候选或未完成的执行工作区
    ├── task.json         完整工单快照（若已创建）
    ├── trajectory.json   Agent 轨迹（若执行返回轨迹）
    ├── candidate.patch   精确候选 diff（若已产生候选）
    └── result.json       独立评分、检查证据和模板配置
```

报告逐次原子更新，保留 provider/model、步数和时间预算、suite hash、复现/验收日志以及每次结果。主要统计包括：

- `pass_rate`：独立评分通过次数 / 所有次数。
- `review_ready`：进入待审查的次数。
- `false_candidate_rate`：待审查但评分失败次数 / 待审查次数；无候选时为 0，须同时看计数。
- `source_mutations`：已确认源版本、分支或工作区变化的次数。
- `source_checks_missing`：源检查未完成的次数，不把基础设施错误当作确认越权。
- `mean_steps`：模型动作步数均值；失败也计入。

退出码：全部评分通过为 `0`，至少一次失败为 `1`，参数错误为 `2`。产物保留在本机，不自动清理或持久化到正在运行的 HTTP 服务；因此这些 task ID 不能直接在另一个工单服务中查询或审批。可查看 patch、轨迹和 Git 分支，或在服务中另建工单。

## 限制

仅有两个小型人工 fixture，不是生产仓库成功率基准；应在相同 suite hash、模型、预算和环境下重复运行，不只保留最好一次。当前没有 token 用量或费用统计。

`--deadline` 使用生产控制器的协作式时间检查，不是硬中断；模型请求、重试或当前命令可能越过边界才停止。评测同步等待工单执行终态，网络卡住时仍受 provider 请求超时/重试限制。审批的重新验收已由 `tests/test_repairs.py` 覆盖，本 CLI 刻意不自动审批。

本地执行器和独立验收不是 OS 沙箱，不能对抗可任意执行代码的恶意进程。源变化检测是事后检查，不拦截写入、不检测修改后恢复，也不会自动回滚；源仓库只指评测创建的 fixture，不扫描宿主机其他路径。临时 fixture 关闭 Git hooks，但生产工单的 hooks 仍需要管理员审查。
