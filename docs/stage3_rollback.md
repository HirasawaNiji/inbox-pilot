# 阶段三：真实受控回滚

本功能用于撤销一个已经成功写入 Outlook 的 InboxPilot 分类动作。它只恢复
`InboxPilot/` 命名空间，绝不会移动、删除、发送邮件，也不会修改主题或正文。

代码与模拟 Graph Client 验收已经完成，并于 2026-08-09 使用一封专用 Outlook 测试邮件完成真实回滚验收。

## 安全边界

- 一次只允许回滚一个明确的 Action ID，不提供批量入口；
- 只接受状态为 `succeeded` 的动作；
- 必须先运行 dry-run，并提交它生成的独立回滚幂等键；
- 必须填写非空原因，并用 `--confirm-action` 重复同一个 Action ID；
- 使用默认关闭的私有 `graph_write.local.yaml` 和独立 `Mail.ReadWrite` 令牌缓存；
- 写前重新 GET 最新分类和 `changeKey`；
- 当前 InboxPilot 分类只要不等于正向动作的目标，就按冲突处理并保持零 PATCH；
- 保留写入后由用户新增的所有非 `InboxPilot/` 分类；
- 单次执行最多发送一次 PATCH，客户端不跟随重定向，也不自动重试；
- PATCH 结果不确定时进入专用状态，只能通过零 PATCH 对账命令处理。

## 第一步：生成本地预览

```powershell
$plan = uv run inbox-agent actions rollback ACTION_ID `
  --reason "分类结果不符合预期" `
  --dry-run `
  --format json | ConvertFrom-Json

$rollbackKey = $plan.plan.rollback_idempotency_key
$rollbackKey
```

预览不会读取或修改 Outlook，`graph_write_request_count` 固定为 `0`。它展示原始
InboxPilot 分类、正向写入后的预期分类、计划新增/移除的分类以及回滚幂等键。

## 第二步：执行一封邮件的真实回滚

确认测试邮件当前状态后执行：

```powershell
uv run inbox-agent actions rollback-execute ACTION_ID `
  --reason "分类结果不符合预期" `
  --rollback-idempotency-key $rollbackKey `
  --confirm-action ACTION_ID `
  --graph-config config/graph_write.local.yaml `
  --format json
```

确认门在配置加载、令牌读取、队列读取和 Graph 请求之前执行。Action ID 不一致时，
命令退出码为 `1`，并明确报告没有发送 Graph 请求。

成功输出的主要结果：

| outcome | 最终状态 | GET | PATCH | 含义 |
| --- | --- | ---: | ---: | --- |
| `rolled_back` | `rolled_back` | 1 | 1 | Graph 已返回并验证恢复后的分类 |
| `no_change` | `rolled_back` | 1 | 0 | 实时分类已经是恢复目标 |
| `already_rolled_back` | `rolled_back` | 0 | 0 | 同一回滚的幂等重放 |
| `conflict` | `rollback_failed` | 1 | 0 | InboxPilot 分类已被其他操作改变 |
| `failed` | `rollback_failed` | 1 | 0 或 1 | 读取失败或 Graph 明确拒绝写入 |
| `outcome_unknown` | `rollback_outcome_unknown` | 1 | 1 | PATCH 可能成功，禁止盲目重试 |

`conflict`、`failed` 和 `outcome_unknown` 使用退出码 `2`；配置、确认、认证或本地持久化
错误使用退出码 `1`。

## 实时恢复算法

dry-run 的最终集合来自动作创建时的历史快照；真实执行不会直接写入该旧集合。
执行器会在写前重新读取实时分类，然后计算：

```text
实时非 InboxPilot 分类 + 动作创建前的 InboxPilot 分类
```

例如：

```text
动作创建前：School, InboxPilot/P5
正向写入后：School, InboxPilot/P1, InboxPilot/security_alert
用户后来新增：AcceptanceKeep
真实回滚目标：School, AcceptanceKeep, InboxPilot/P5
```

因此 `AcceptanceKeep` 会被保留。写前读取到的分类、目标分类、`changeKey`、回滚原因和幂等键
会持久化到私有动作队列的 `rollback_snapshot`，供崩溃后对账使用；公开审计日志不保存分类列表
或原始 Message ID。

## 结果不确定时的对账

若网络在 PATCH 后中断，不要再次运行 `rollback-execute`。执行：

```powershell
uv run inbox-agent actions rollback-reconcile ACTION_ID `
  --rollback-idempotency-key $rollbackKey `
  --graph-config config/graph_write.local.yaml `
  --format json
```

该命令固定执行一次 GET、零次 PATCH：

- 实时分类等于持久化目标：`applied` → `rolled_back`；
- 实时分类等于 PATCH 前快照：`not_applied` → `rollback_failed`；
- 两者都不等：`conflict` → `rollback_failed`；
- GET 失败：`read_failed`，保留不确定状态以便稍后再次只读对账。

## 回滚状态机

```text
succeeded
  -> rollback_executing
  -> rollback_write_in_flight
  -> rolled_back | rollback_failed | rollback_outcome_unknown

rollback_outcome_unknown
  -> rolled_back | rollback_failed       # 只能由只读对账决定

rollback_failed
  -> rollback_executing                  # 显式重试，仍需同一安全校验
```

`rolled_back` 是终态。真实 PATCH 前必须先持久化 `rollback_write_in_flight` 和
`rollback_snapshot`；若这一步或对应审计不能落盘，执行器不会发送 PATCH。

## 本地自动化验收

```powershell
uv run pytest tests/test_action_rollback.py `
  tests/test_action_rollback_executor.py `
  tests/test_action_models.py `
  tests/test_action_queue.py `
  tests/test_cli.py -q
```

自动化测试全部使用模拟 Graph Client，不会访问真实 Outlook。真实验收应只使用一封可丢弃的
测试邮件，并在回滚后确认：原始 InboxPilot 分类已恢复、用户分类仍存在、邮件未移动或删除、
主题与正文没有变化。

## 真实 Outlook 验收记录

2026-08-09 对专用测试动作执行了一次真实受控回滚：

- 写前实时分类为 `acceptancekeep`、`InboxPilot/P4`、`InboxPilot/general_notice`；
- 回滚目标仅为 `acceptancekeep`；
- Graph 请求计数为一次 GET、一次 PATCH，自动重试为零；
- 队列状态依次经过 `rollback_executing`、`rollback_write_in_flight`，最终为 `rolled_back`；
- 用户在 Outlook 中确认 `acceptancekeep` 得到保留，两项 InboxPilot 分类已移除；
- 邮件未被移动、删除或发送，主题和正文没有变化。

因此真实单封回滚验收正式通过。真实 Message ID、令牌、邮件正文和私有队列内容不进入仓库。
