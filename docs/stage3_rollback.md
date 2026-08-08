# 阶段三步骤六：受控回滚计划

步骤六为已经成功执行的 InboxPilot 分类动作生成本地恢复计划。当前功能严格限制为 dry-run：不申请 `Mail.ReadWrite`，不构造 Graph 写请求，不修改 Outlook，也不会把动作提前标记为 `rolled_back`。

## 使用条件

回滚计划必须同时满足：

- 明确指定一个动作 ID，不支持批量回滚；
- 动作状态必须是 `succeeded`；
- 动作必须具有步骤五生成并校验过的前向幂等键；
- 用户必须通过 `--reason` 提供非空原因；
- 必须显式传入 `--dry-run`。

命令示例：

```powershell
uv run inbox-agent actions rollback ACTION_ID `
  --reason "分类结果不符合预期" `
  --dry-run
```

输出机器可读 JSON：

```powershell
uv run inbox-agent actions rollback ACTION_ID `
  --reason "分类结果不符合预期" `
  --dry-run `
  --format json
```

自定义私有文件路径：

```powershell
uv run inbox-agent actions rollback ACTION_ID `
  --reason "测试恢复计划" `
  --dry-run `
  --queue data/private/action_queue.test.json `
  --audit-log data/private/audit/test-actions.jsonl
```

省略 `--dry-run` 时，命令以退出码 `1` 拒绝继续。动作不是 `succeeded`、不存在、缺少幂等键或原因仅包含空白时也会失败。

## 恢复目标

前向动作只管理 `InboxPilot/` 命名空间。回滚计划把动作创建时的分类快照分成两组：

- 原始用户分类，例如 `School`、`Important`；
- 原始 InboxPilot 分类，例如 `InboxPilot/P3`。

计划先推导前向动作成功后的预期状态：

```text
原始用户分类 + 前向动作的 InboxPilot 分类
```

然后计算恢复到原始快照所需的精确差异：

- `add_categories`：原始快照存在、预期写后状态缺少的 InboxPilot 分类；
- `remove_categories`：前向动作增加、原始快照不存在的 InboxPilot 分类；
- `final_categories`：动作创建时的原始分类快照；
- `would_write`：上述差异是否为空。

例如：

```text
原始：School, Important, InboxPilot/P5, InboxPilot/old_notice
预期写后：School, Important, InboxPilot/P1, InboxPilot/security_alert
恢复新增：InboxPilot/P5, InboxPilot/old_notice
恢复移除：InboxPilot/P1, InboxPilot/security_alert
最终：School, Important, InboxPilot/P5, InboxPilot/old_notice
```

该计划始终保留原始快照中的非 InboxPilot 分类，不会计划删除用户自己的分类。

## 回滚幂等键

每个计划具有独立的 `rollback_idempotency_key`。它由以下语义输入生成 SHA-256：

- 前向动作 ID；
- 前向动作幂等键；
- 预期写后分类集合；
- 回滚最终分类集合。

分类集合排序后再参与哈希，因此单纯改变排列不会改变键。用户原因和生成时间不属于写入语义，同一恢复操作更换说明或再次预览时仍得到相同键。加载计划时会重新计算并校验，篡改分类差异或幂等键会被 Schema 拒绝。

## 快照与真实状态的边界

回滚 dry-run 基于动作生成时的快照和前向写入计划，并没有读取当前 Outlook 邮件。`original_change_key` 仅用于说明原始观察版本，不能直接作为未来回滚写入的并发依据。

真实回滚执行器必须在写入前：

1. 重新读取邮件的最新分类与 `changeKey`；
2. 确认当前 InboxPilot 分类与被回滚动作之间没有冲突；
3. 保留动作生成后用户新增加的所有非 InboxPilot 分类；
4. 重新计算实际差异；
5. 通过严格 Graph URL 和方法 allowlist 执行；
6. 只有 Graph 成功后，才能由 `system` 把动作标记为 `rolled_back`。

因此，当前计划是可审查的恢复意图，不是可直接发送的 Graph 请求。

## 审计与状态安全

每次成功生成回滚预览会追加 `rollback_dry_run_planned` 审计事件，包含：

- 动作 ID 与哈希后的邮件 ID；
- `succeeded` 源状态；
- 用户提供的原因；
- 分类新增、移除和数量；
- 固定为 `0` 的 Graph 写请求数。

审计事件不包含邮件主题、正文、摘要、原始邮件 ID、令牌或 API Key。原因属于用户输入，应避免填写邮件正文或其他敏感内容。

回滚 dry-run 不改变队列文件和动作状态。状态机要求 `rolled_back` 只能由 `system` 在未来真实恢复成功后写入，并且必须附带说明；用户请求或 dry-run 不能提前声明回滚已经完成。

## 当前安全边界

- Graph 权限保持 `Mail.Read`；
- `graph_write_request_count` 由类型固定为 `0`；
- CLI 测试使用一旦构造就失败的 Graph 客户端替身，回滚 dry-run 仍能通过；
- 命令执行前后队列文件字节保持一致；
- 当前没有 Graph 回滚执行器，也没有真实 `rolled_back` CLI。

阶段三前半的综合结果见[离线验收报告](stage3_front_half_acceptance.md)。
