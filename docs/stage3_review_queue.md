# 阶段三步骤二：本地人工确认队列

人工确认队列把邮件分析结果转换为本地 `MailboxAction`，让用户在任何 Outlook 写回发生前逐项检查、批准或拒绝。本步骤仍只使用 Microsoft Graph `Mail.Read`，所有命令都不会修改邮箱。

## 默认存储位置

队列默认保存在：

```text
data/private/action_queue.json
```

`data/private/` 已被 `.gitignore` 排除。队列包含邮件 ID、摘要、现有分类、规则与可选 LLM 证据，因此不应复制到 Git、Issue、PR 或公开日志。

队列使用严格 Pydantic Schema 加载，并通过同目录临时文件原子替换。缺失文件被视为空队列；非法 JSON、未知字段、重复动作 ID 或状态历史损坏都会被拒绝。

## 生成队列

先使用公开虚构样例验证流程：

```powershell
uv run inbox-agent actions build
```

从本地只读同步的 Outlook 数据生成队列：

```powershell
uv run inbox-agent actions build `
  --dataset data/private/outlook_inbox.json
```

若希望分析时使用真实 LLM，可以显式传入本地 Provider 配置：

```powershell
uv run inbox-agent actions build `
  --dataset data/private/outlook_inbox.json `
  --llm-config config/llm_provider.local.yaml
```

这会把邮件正文发送给所选 LLM Provider，必须先确认隐私与费用边界。不传 `--llm-config` 时完全离线运行规则分析。

`actions build` 会为每个成功分析结果生成一个 `pending_review` 分类动作。同一邮件快照、分类建议和规则版本会产生稳定动作 ID；再次构建会显示为“重复跳过”，不会覆盖已批准或已拒绝的状态。如果 Outlook `changeKey`、现有分类、最终分类或规则版本发生变化，则会生成新的待确认动作。

## 查看队列

查看全部动作：

```powershell
uv run inbox-agent actions list
```

只看待确认动作：

```powershell
uv run inbox-agent actions list --status pending_review
```

输出 JSON：

```powershell
uv run inbox-agent actions list --format json
```

查看某一动作的分类计划与规则/LLM 证据：

```powershell
uv run inbox-agent actions show action-xxxxxxxxxxxxxxxxxxxxxxxx
```

表格会显示现有 Outlook 分类和建议添加的 `InboxPilot/` 分类。当前阶段不会计算或发送 Graph PATCH 请求。

## 批准与拒绝

批准：

```powershell
uv run inbox-agent actions approve action-xxxxxxxxxxxxxxxxxxxxxxxx `
  --note "分类和优先级正确"
```

拒绝：

```powershell
uv run inbox-agent actions reject action-xxxxxxxxxxxxxxxxxxxxxxxx `
  --reason "保留原有分类"
```

批准只把状态从 `pending_review` 改为 `approved`；它不等于执行，也不会连接 Graph。后续 dry-run 只读取 `approved` 动作。重复批准、批准已拒绝动作或非法状态跳转会返回错误，不会修改队列。

## 自定义队列路径

所有命令都支持 `--queue`：

```powershell
uv run inbox-agent actions list `
  --queue data/private/my_action_queue.json
```

建议始终把队列放在 `data/private/`。如果选择其他位置，开发者必须自行确认该路径被 Git 忽略。

## 当前边界

- 不执行 Graph 写请求；
- 不申请 `Mail.ReadWrite`；
- 不移动、删除、发送或标记邮件；
- 不批量自动批准；
- 不覆盖同一动作已有的人工决定；
- 原子替换避免半写入 JSON；步骤五又为完整的读取—修改—写入事务增加跨进程文件锁；
- 动作生成和状态迁移同时保存在动作历史与追加式审计日志中。

步骤三已实现已批准动作的分类 dry-run，并用测试证明 Graph 写客户端调用次数始终为零。使用方法见[Outlook 分类 dry-run](stage3_dry_run.md)。步骤四已将动作生成和人工状态变化接入[追加式动作审计日志](stage3_audit_log.md)。
