# 阶段四步骤二：统一工作流编排

步骤二把 InboxPilot 已有的 JSON/Outlook 输入、标准化、规则分析、可选 LLM、结果持久化和
人工确认动作连接成一条可重复运行的本地工作流。工作流的终点始终是人工确认队列，绝不会
自动执行 Outlook 写回。

## 工作流顺序

```text
可选 Outlook 只读增量同步
→ 加载 MessageDataset
→ 幂等导入 SQLite 并保存标准化快照
→ 选择新增、变更或需要重试的邮件
→ 规则分析与可选 LLM 路由/融合
→ 保存分析结果
→ 生成并去重人工确认动作
→ 写入私有审计日志
```

每次运行都会在 `workflow_runs` 中保存 Run ID、当前步骤、步骤结果、计数、失败摘要和结束
状态。即使进程在中途失败，`workflow status` 仍能显示最后完成到哪里。

## 离线运行

先初始化或升级数据库：

```powershell
uv run inbox-agent db init
```

处理公开的 50 封样例：

```powershell
uv run inbox-agent workflow run `
  --dataset data/samples/sample_emails.json `
  --database data/private/stage4-test.sqlite3 `
  --queue data/private/stage4-test-actions.json `
  --audit-log data/private/stage4-test-audit.jsonl `
  --format json
```

公开样例必须使用独立测试路径，避免将 50 个样例动作混入此前真实 Outlook 验收使用的默认队列。
第二次验收时原样重复上面的命令。

第一次运行会分析 50 封邮件并生成待确认动作。第二次执行相同命令时，邮件内容与分析配置
没有变化，因此预期：

- `eligible_messages` 为 0；
- `skipped_current` 为 50；
- `analyzed_messages` 为 0；
- `actions_generated` 为 0；
- `graph_write_request_count` 始终为 0。

## 接入真实 LLM

```powershell
uv run inbox-agent workflow run `
  --dataset data/private/outlook_inbox.json `
  --llm-config config/llm_provider.local.yaml `
  --format json
```

分析档案 `analysis_profile` 会对以下内容计算 SHA-256：

- `rules.yaml` 内容；
- Provider 名称；
- 模型名称；
- Prompt 版本；
- LLM 路由配置；
- LLM 融合配置。

修改其中任意一项后，即使邮件内容不变，也会建立新的分析档案并重新分析。API Key 只从环境
变量读取，不参与哈希，也不会写入数据库。

如果某封邮件的 LLM 调用失败：

- 其他邮件继续处理；
- 失败邮件的规则结果可以保存，但不会生成新的动作；
- 该分析不会标记为完成；
- 下一次运行只重试失败邮件；
- 命令以退出码 2 表示“工作流完成但存在隔离失败”。

## 一条命令同步并分析 Outlook

确保已经完成只读登录，然后运行：

```powershell
uv run inbox-agent workflow run `
  --sync-outlook `
  --graph-config config/graph.local.yaml `
  --format json
```

此命令先执行现有 Microsoft Graph Inbox Delta 只读同步，再分析同步产生的私有 JSON 数据集。
它只使用 `Mail.Read` 授权，不读取写权限配置，也不会创建 Graph PATCH 请求。

真实 LLM 与 Outlook 同步可以组合：

```powershell
uv run inbox-agent workflow run `
  --sync-outlook `
  --graph-config config/graph.local.yaml `
  --llm-config config/llm_provider.local.yaml `
  --format json
```

## 查看运行状态

```powershell
uv run inbox-agent workflow status
uv run inbox-agent workflow status --format json
```

状态包含：

- 最近 Run ID；
- `running`、`completed`、`completed_with_failures` 或 `failed`；
- 当前步骤；
- 开始和结束时间；
- 邮件、失败、动作和 Graph 写请求计数；
- 每个步骤的完成状态。

`workflow status` 不读取邮件正文；数据库不存在时也不会创建空文件。

## 强制重新分析

调试 Prompt 或规则时可以使用：

```powershell
uv run inbox-agent workflow run `
  --dataset data/samples/sample_emails.json `
  --force
```

`--force` 会重新调用分析流水线。结果仍按邮件内容和分析档案幂等保存，人工动作仍按稳定
Action ID 去重。真实 LLM 下使用此参数会产生新的 API Token 消耗。

## 数据库升级

步骤二新增迁移 `0002_workflow`。如果已经运行过步骤一，不需要删除 `0001_stage4` 数据库：

```powershell
uv run inbox-agent db init
uv run inbox-agent db status --format json
```

Alembic 会保留已有邮件和分析记录，并补充内容哈希、分析档案、完成状态以及工作流步骤字段。

## 安全边界

- 默认不连接网络；
- `--sync-outlook` 只调用只读 Graph 同步；
- 工作流不导入 `GraphCategoryWriteClient`；
- 所有建议只进入人工确认队列；
- `graph_write_request_count` 的模型约束固定为 0；
- LLM 失败不会阻止其他邮件，也不会自动降级为无人确认写回；
- 数据库、动作队列、审计日志、真实邮件和同步游标均位于 Git 忽略路径。

## 快速验收

```powershell
uv run inbox-agent db init `
  --database data/private/stage4-test.sqlite3 `
  --format json

uv run inbox-agent workflow run `
  --dataset data/samples/sample_emails.json `
  --database data/private/stage4-test.sqlite3 `
  --queue data/private/stage4-test-actions.json `
  --audit-log data/private/stage4-test-audit.jsonl `
  --format json

uv run inbox-agent workflow run `
  --dataset data/samples/sample_emails.json `
  --database data/private/stage4-test.sqlite3 `
  --queue data/private/stage4-test-actions.json `
  --audit-log data/private/stage4-test-audit.jsonl `
  --format json

uv run inbox-agent workflow status `
  --database data/private/stage4-test.sqlite3 `
  --format json
```

验收重点：第一次分析 50 封；第二次跳过 50 封；状态为 `completed`；Graph 写请求始终为 0。
