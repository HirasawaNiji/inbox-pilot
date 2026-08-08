# 阶段三后半步骤三：写前重读与受控执行器

本步骤把人工确认队列、幂等认领、Graph 写前读取和分类写客户端组合为单动作执行器。后半步骤五已经提供受确认门保护的单动作 CLI；所有自动化验收仍使用模拟 Graph Client，开发过程中没有修改真实 Outlook 邮件。

## 执行顺序

`ApprovedActionGraphExecutor` 严格按照以下顺序处理一个动作：

1. 使用动作 ID 和幂等键调用 `claim_execution()`；
2. 只有已经人工批准的动作才能进入 `executing`；
3. 使用 Immutable Message ID 重新读取 `id`、`categories` 和 `changeKey`；
4. 将实时分类和 `changeKey` 与人工批准时的快照比较；
5. 若完全一致，计算保留用户分类后的最终分类集合；
6. 若无需变化，直接记录 `succeeded`，Graph 写请求为零；
7. 若需要写入，先持久化 `write_in_flight`，再发送一次分类 PATCH；
8. 验证 Graph 响应后记录 `succeeded`，否则进入明确的失败或结果未知状态。

执行器不会处理整个队列，也不会自行选择动作。调用者必须明确提供一个动作 ID 和该动作的幂等键。

## 写前冲突检查

以下任意情况都会产生 `conflict`，动作进入 `failed`，Graph PATCH 数量保持为零：

- 人工批准时的快照没有 `changeKey`；
- 实时 `changeKey` 与批准快照不同；
- 实时分类集合与批准快照不同。

分类比较忽略顺序和大小写，避免仅由 Graph 返回顺序造成误冲突。只要用户增加、删除或修改了任意分类，执行器就不会使用旧快照覆盖用户的新状态。

`changeKey` 可能因为分类之外的邮件属性变化而改变，因此该检查可能产生保守的额外冲突。这是有意设计：宁可要求重新分析和人工批准，也不在状态不明确时写入。

## 防崩溃状态

状态机增加两个写入安全状态：

```text
approved
   |
   v
executing        # 只进行写前读取，尚未发送 PATCH
   |
   v
write_in_flight  # 已准备发送或可能已经发送 PATCH
   |       \
   v        v
succeeded  outcome_unknown
```

- `executing` 陈旧后仍可以按原有机制恢复，因为执行器在该状态下尚未发送 PATCH；
- `write_in_flight` 永远不能通过陈旧租约直接重新认领；
- 网络中断、成功响应无法验证等情况进入 `outcome_unknown`；
- `outcome_unknown` 同样不能重新认领，必须先重新读取邮件进行对账；
- 如果 PATCH 成功但队列成功状态无法落盘，动作保持 `write_in_flight`，并抛出 `ActionExecutionPersistenceError`，阻止调用者把它当成普通可重试失败。

在持久化 `write_in_flight` 失败时，执行器保证不会发送 PATCH。

## 执行结果

`ActionGraphExecutionReport` 只记录动作 ID、结果类型、最终状态、尝试次数、读写请求数量和安全错误原因，不包含访问令牌、邮件正文、主题、原始 Message ID 或分类列表。

结果类型包括：

| 结果 | Graph GET | Graph PATCH | 队列状态 |
| --- | ---: | ---: | --- |
| `already_succeeded` | 0 | 0 | `succeeded` |
| `no_change` | 1 | 0 | `succeeded` |
| `succeeded` | 1 | 1 | `succeeded` |
| `conflict` | 1 | 0 | `failed` |
| `failed` | 1 | 0 或 1 | `failed` |
| `outcome_unknown` | 1 | 1 | `outcome_unknown` |

执行器本身不进行自动重试。普通 `failed` 动作可以在问题解决后重新认领，但每次都必须重新完成写前读取。`write_in_flight` 和 `outcome_unknown` 不能使用该重试通道。

## 离线验收

```powershell
uv run pytest tests/test_action_graph_executor.py tests/test_action_execution.py tests/test_graph_write_client.py -q
```

测试覆盖：

- 只允许已批准动作到达 Graph；
- GET 必须先于 PATCH；
- `changeKey` 或分类变化时写请求为零；
- 保留全部非 `InboxPilot/` 分类；
- 无变化时成功且 PATCH 为零；
- 明确 Graph 失败不会在同次执行中重试；
- 结果未知后无法盲目重试；
- 成功动作再次执行为零请求 no-op；
- `write_in_flight` 无法通过陈旧租约夺取；
- 写前状态无法持久化时不发送 PATCH；
- PATCH 后队列落盘失败时不返回普通失败。

## 尚未实现

- [x] 后半步骤五已提供受确认门保护的单动作真实执行 CLI；
- [x] 后半步骤四已将执行状态和 Graph 操作结果追加到审计 JSONL；
- [x] 后半步骤四已实现 `write_in_flight` / `outcome_unknown` 的只读对账器；
- [x] 后半步骤五已提供单动作执行和固定零 PATCH 的只读对账 CLI；
- 没有批量上限和真实 Outlook 小批量验收；
- 没有执行真实回滚 PATCH。

执行审计和明确对账流程见[后半步骤四](stage3_execution_audit_and_reconciliation.md)，命令入口见[后半步骤五](stage3_single_action_cli.md)。下一步是完成单封测试邮箱验收，再设计小批量边界。

## 官方参考

- [outlookItem 的 changeKey 定义](https://learn.microsoft.com/en-us/graph/api/resources/outlookitem?view=graph-rest-1.0)
- [获取邮件资源](https://learn.microsoft.com/en-us/graph/api/message-get?view=graph-rest-1.0)
- [更新邮件资源](https://learn.microsoft.com/en-us/graph/api/message-update?view=graph-rest-1.0)
