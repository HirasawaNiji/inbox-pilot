# 阶段三步骤三：Outlook 分类 dry-run

dry-run 把本地人工确认队列中的 `approved` 动作转换为精确分类差异，但不构造 Graph 客户端、不发送 HTTP 请求、不修改动作状态，也不改变队列文件。

## 运行命令

先生成队列并批准至少一个动作：

```powershell
uv run inbox-agent actions build
uv run inbox-agent actions list --status pending_review
uv run inbox-agent actions approve ACTION_ID
```

预览已批准动作：

```powershell
uv run inbox-agent actions apply --dry-run
```

机器可读输出：

```powershell
uv run inbox-agent actions apply --dry-run --format json
```

使用自定义队列：

```powershell
uv run inbox-agent actions apply --dry-run `
  --queue data/private/action_queue.test.json
```

如果省略 `--dry-run`，命令会以退出码 `1` 拒绝执行，并说明真实 Graph 写入器尚未启用。

## 计划内容

每个已批准动作生成一条 `ActionDryRunPlan`：

- `current_categories`：生成动作时只读同步到的 Outlook 分类；
- `managed_categories`：最终判断对应的 `InboxPilot/P1`、类别和可选 `InboxPilot/review`；
- `add_categories`：当前快照中不存在、未来需要新增的 InboxPilot 分类；
- `remove_categories`：当前存在但不再符合建议的旧 InboxPilot 分类；
- `final_categories`：保留全部用户分类后得到的完整目标集合；
- `would_write`：未来执行器是否需要发送分类更新；
- `change_key`：生成动作时观察到的 Graph 版本标识，供后续陈旧状态检查使用。

例如：

```text
当前：School, Important, InboxPilot/P3
新增：InboxPilot/P1, InboxPilot/security_alert, InboxPilot/review
移除：InboxPilot/P3
最终：School, Important, InboxPilot/P1, InboxPilot/security_alert, InboxPilot/review
```

`School` 和 `Important` 属于用户分类，必须原样保留。只有大小写不敏感匹配 `InboxPilot/` 前缀的分类可以被计划移除。

## 批次报告

`DryRunReport` 包含：

- 队列动作总数；
- 符合条件的已批准动作数；
- 因未批准而跳过的动作数；
- 未来需要写入的动作数；
- 已经符合目标、无需写入的动作数；
- `graph_write_request_count`，当前被类型固定为 `0`。

只有 `approved` 动作会出现在计划中。`pending_review`、`rejected`、`failed` 等状态全部跳过。

## 安全保证

- dry-run 模块不导入 `GraphMailClient`；
- CLI 测试将 Graph 客户端替换为一旦构造就失败的测试替身，dry-run 仍能通过；
- 报告 Schema 不允许 `graph_write_request_count` 大于零；
- dry-run 前后队列文件字节保持一致；
- 用户原有非 InboxPilot 分类必须全部出现在最终集合中；
- 分类差异与 `would_write` 由模型交叉校验，不能提交互相矛盾的报告；
- 当前 Graph 权限继续保持 `Mail.Read`。

每条 dry-run 计划还会写入[追加式动作审计日志](stage3_audit_log.md)，但不会改变动作状态或队列内容。

## 当前限制

dry-run 使用动作创建时保存的分类快照。步骤五已实现幂等键、并发文件锁和陈旧执行租约恢复；后半阶段执行器与单动作 CLI 会在真实写入前重新读取邮件当前分类并比较 `changeKey`，不会直接把旧计划写入邮箱。

步骤四已为动作生成、批准、拒绝和 dry-run 建立追加式审计日志；步骤五的幂等、并发保护与安全重试见[专项说明](stage3_idempotency_and_retry.md)；步骤六的恢复预览见[受控回滚说明](stage3_rollback.md)。
