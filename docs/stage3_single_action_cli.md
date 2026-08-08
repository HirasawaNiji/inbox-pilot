# 阶段三后半步骤五：单动作执行与只读对账 CLI

本步骤把已完成的受控执行器和对账器接入命令行。`actions execute` 具备真实修改 Outlook 单封邮件分类的能力；`actions reconcile` 只读取分类并修复本地不确定状态。自动化测试使用假 Graph Client；2026-08-09 另使用个人 Outlook 中的专用合成测试邮件完成单封真实验收。

## 安全边界

`actions execute` 同时满足以下条件才会继续：

1. 只提供一个明确的 Action ID；
2. 提供该动作批准后保存的精确幂等键；
3. 使用 `--confirm-action` 再输入一次完全相同的 Action ID；
4. 私有 `graph_write.local.yaml` 中显式设置 `write_enabled: true`；
5. 独立的 `Mail.ReadWrite` 令牌缓存能够静默提供令牌；
6. 队列中的动作处于执行器允许的状态，且写前实时分类与批准快照一致。

确认值缺失或不一致时，命令会在读取 Graph 配置、获取令牌、打开队列和构造 HTTP Client 之前退出，因此不会发送 Graph 请求，也不会修改队列。

该入口没有以下能力：

- 批量执行；
- 交互式登录或隐式授权；
- 对未知写结果自动重试；
- 移动、删除、发送邮件或修改除 `categories` 以外的字段；
- 绕过写前 `changeKey` 和分类快照检查。

## 执行前准备

先完成独立写授权：

```powershell
Copy-Item config/graph_write.example.yaml config/graph_write.local.yaml
# 编辑本地文件，填写 client_id，并在理解风险后设置 write_enabled: true
uv run inbox-agent outlook write-login --config config/graph_write.local.yaml
```

`graph_write.local.yaml` 和独立令牌缓存均已由 `.gitignore` 排除。不要把访问令牌、API Key 或真实邮件内容写入命令参数、文档或 Git。

## 读取动作 ID 与幂等键

先只读查看一个已经人工批准的动作：

```powershell
$actionId = "action-..."
$action = uv run inbox-agent actions show $actionId --format json | ConvertFrom-Json
$idempotencyKey = $action.idempotency_key
$action.status
$idempotencyKey
```

只有状态和分类计划均符合预期时才继续。建议先运行现有 dry-run：

```powershell
uv run inbox-agent actions apply --dry-run --format json
```

## 执行一个动作

以下命令可能真实修改一封 Outlook 邮件的分类：

```powershell
uv run inbox-agent actions execute $actionId `
  --idempotency-key $idempotencyKey `
  --confirm-action $actionId `
  --graph-config config/graph_write.local.yaml `
  --format json
```

执行器最多进行一次写前 GET 和一次 PATCH。输出中的 `graph_read_request_count` 与 `graph_write_request_count` 是本次操作的请求计数证据。

| `outcome` | 含义 | 退出码 |
| --- | --- | ---: |
| `succeeded` | PATCH 响应验证通过，队列已保存成功 | 0 |
| `no_change` | 实时分类已是目标状态，没有 PATCH | 0 |
| `already_succeeded` | 同一动作已成功，幂等返回，没有 Graph 请求 | 0 |
| `conflict` | 批准后的邮件状态发生变化，没有 PATCH | 2 |
| `failed` | Graph 读取或写入明确失败 | 2 |
| `outcome_unknown` | PATCH 可能已生效，不允许盲目重试 | 2 |

配置、认证、确认、队列、审计或持久化错误返回退出码 `1`。

## 对账不确定动作

只有动作状态为 `write_in_flight` 或 `outcome_unknown` 时才使用：

```powershell
uv run inbox-agent actions reconcile $actionId `
  --idempotency-key $idempotencyKey `
  --graph-config config/graph_write.local.yaml `
  --format json
```

对账固定发送一次 GET、零次 PATCH：

| `outcome` | 结论 | 最终状态 | 退出码 |
| --- | --- | --- | ---: |
| `applied` | 实时分类等于批准目标 | `succeeded` | 0 |
| `not_applied` | 实时分类等于批准前快照 | `failed` | 0 |
| `conflict` | 实时分类与两份快照均不同 | `failed` | 2 |
| `read_failed` | 无法得出结论，保留不确定状态 | 不变 | 2 |

`not_applied` 返回 0 表示“对账已得到确定结论”，不表示动作写入成功。

## 离线验收

下面的测试不会连接真实 Outlook：

```powershell
uv run pytest tests/test_cli.py tests/test_action_graph_executor.py -q
```

重点验证：

- 确认门失败时配置加载、令牌获取、队列修改和 Graph 请求均为零；
- 执行只使用静默令牌，并且一次只传递一个 Action ID；
- JSON 输出可被 PowerShell `ConvertFrom-Json` 直接读取；
- 冲突和不确定结果使用退出码 2；
- 对账报告的 Graph 写请求数固定为零。

## 尚未完成

- 小批量受控验收和批次上限设计；
- 真实回滚 PATCH；
- 跨队列与审计文件的数据库级事务。

单封真实验收的范围、结果和升级兼容性修复见[个人 Outlook 单封真实写入验收](stage3_single_message_acceptance.md)。在完成小批量边界和真实回滚前，不应把该命令用于重要邮件或批量操作。
