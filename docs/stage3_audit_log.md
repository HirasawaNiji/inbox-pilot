# 阶段三步骤四：追加式动作审计日志

InboxPilot 为动作生成、人工状态变更和 dry-run 计划写入严格 JSONL 审计日志。日志默认位于：

```text
data/private/audit/actions.jsonl
```

该目录已被 `.gitignore` 排除。审计功能不会申请 Graph 写权限，也不会修改邮箱。

## 事件类型

当前记录五类事件：

| 事件 | 含义 |
| --- | --- |
| `action_generated` | 分析结果首次形成 `pending_review` 动作 |
| `action_status_changed` | 用户批准、拒绝或未来执行器改变动作状态 |
| `dry_run_planned` | 某个已批准动作被纳入一次分类 dry-run |
| `rollback_dry_run_planned` | 用户为某个成功动作显式生成回滚预览 |
| `graph_operation_recorded` | 记录受控执行或只读对账的结果与 Graph 读写请求数量 |

状态事件保存 `from_status`、`to_status`、行为主体、发生时间和可选说明。dry-run 事件保存分类数量、计划新增/移除的 InboxPilot 分类、是否需要未来写入，以及固定为零的 Graph 写请求数；回滚预览事件还要求用户原因，但不会产生状态迁移。Graph 操作事件只保存执行/对账类型、结果、尝试次数和读写请求计数。

## 隐私字段边界

审计事件允许保存：

- 动作 ID；
- 原始邮件 ID 的 SHA-256；
- 动作类型和状态；
- 优先级、类别、是否复核和决策来源；
- 规则版本；
- 可选 Provider、模型、Prompt 版本和请求 ID；
- 分类差异和人工说明。
- Graph 操作类型、隐私安全结果和读写请求计数。

审计 Schema 不包含以下字段：

- 原始邮件 ID；
- 主题、正文、摘要和附件；
- API Key；
- Graph Access Token 或刷新令牌；
- 邮箱密码。

日志依然属于私有运行数据，因为动作 ID、分类和决策元数据可能泄露使用习惯，不应上传到 Git、Issue、PR 或公开日志平台。

## 追加与去重

每条事件单独占据一行 JSON。写入器使用追加模式，写入后执行 flush 和 `fsync`，不会重写已有历史。

事件 ID 根据动作、事件类型、时间和状态变化生成。相同事件再次出现时会被安全跳过；如果相同事件 ID 对应不同内容，日志会被判定为冲突并停止写入。

加载日志时会逐行执行严格 Pydantic 校验。以下情况会被拒绝：

- 非 UTF-8；
- 非法 JSON；
- 空白行；
- 未知字段；
- 非法枚举或时间；
- 重复事件 ID；
- 与事件类型不一致的 actor、状态或 dry-run 字段。

## CLI 接入

下列命令自动写审计日志：

```powershell
uv run inbox-agent actions build
uv run inbox-agent actions approve ACTION_ID
uv run inbox-agent actions reject ACTION_ID
uv run inbox-agent actions apply --dry-run
```

自定义日志位置：

```powershell
uv run inbox-agent actions apply --dry-run `
  --audit-log data/private/audit/test-actions.jsonl
```

如果使用自定义队列，建议同时指定配套审计文件：

```powershell
uv run inbox-agent actions build `
  --queue data/private/action_queue.test.json `
  --audit-log data/private/audit/test-actions.jsonl
```

`actions build` 会根据当前队列的完整动作历史重新推导确定性事件，因此可以补齐此前因审计文件暂时不可写而缺失的生成或状态事件，同时不会复制已有记录。dry-run 事件代表一次具体预览；如果写审计失败，命令会报错，重新运行 dry-run 会产生新的预览事件。

## 查看本地日志

PowerShell 可以读取最近事件：

```powershell
Get-Content data/private/audit/actions.jsonl -Tail 10
```

不要将输出直接粘贴到公开聊天或 Issue。后续可以增加专用的脱敏审计查询 CLI，而不是依赖原始文件输出。

## 当前边界

- JSONL 的去重检查与追加现在受步骤五的跨进程文件锁保护，多个守规仓储调用者不会互相覆盖；
- 队列 JSON 和审计 JSONL 是两个本地文件，磁盘故障时不能形成跨文件数据库事务；
- 确定性事件和 `actions build` 的历史补齐可以恢复大多数中断场景；
- 回滚 dry-run 已记录为 `rollback_dry_run_planned`；分类执行和不确定结果对账已记录为 `graph_operation_recorded`，真实回滚完成事件仍待扩展；
- 日志轮换、归档和保留周期尚未实现。

后半步骤四已把该日志接入受控执行器和只读对账器，详见[执行审计与对账](stage3_execution_audit_and_reconciliation.md)。
