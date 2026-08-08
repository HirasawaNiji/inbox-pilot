# 阶段三后半步骤四：执行审计与不确定结果对账

本步骤为受控执行器补齐 Graph 操作审计，并为 `write_in_flight` / `outcome_unknown` 提供只读对账器。所有验收仍使用模拟 Graph Client，不会连接或修改真实 Outlook。

## Graph 操作审计

执行器和对账器现在会在原有状态迁移事件之外追加：

```text
graph_operation_recorded
```

事件中的 `graph_operation` 只保存：

- 操作类型：`execute` 或 `reconcile`；
- 结果类型；
- 执行尝试次数；
- Graph GET 请求数；
- Graph PATCH 请求数。

事件不会保存原始 Message ID、分类列表、邮件主题、正文、访问令牌或 API Key。动作事件仍只保存 Message ID 的 SHA-256。

典型执行审计证据：

| 结果 | read | write |
| --- | ---: | ---: |
| 写入成功 | 1 | 1 |
| 无需变化 | 1 | 0 |
| 写前冲突 | 1 | 0 |
| 结果未知 | 1 | 1 |
| 已成功动作再次调用 | 0 | 0 |

### 审计失败时的停止规则

- 动作认领后的审计无法落盘：停止在 `executing`，不读取 Graph；
- `write_in_flight` 审计无法落盘：不发送 PATCH；
- 终态审计无法落盘：队列状态仍保留，可根据完整动作历史重新推导并补齐确定性事件；
- 审计事件通过 `append_unique()` 去重，补写不会产生重复事件。

队列 JSON 与审计 JSONL 仍是两个文件，无法提供数据库级跨文件事务。因此调用方必须把 `ActionExecutionAuditError` 视为需要本地恢复的硬错误，不能继续下一个写动作。

## 只读对账器

`UncertainActionReconciler` 只接受以下状态：

- `write_in_flight`；
- `outcome_unknown`。

对账流程只发送一次 Graph GET，PATCH 数量固定为零：

1. 使用动作 ID 和幂等键加载不确定动作；
2. 先补齐该动作已有状态的审计事件；
3. 使用 Immutable Message ID 读取实时分类和 `changeKey`；
4. 将实时分类分别与“批准前分类”和“计划最终分类”比较；
5. 原子保存对账结论并追加状态与 Graph 操作审计。

## 对账结论

| 实时分类 | 对账结果 | 最终状态 | Graph 写请求 |
| --- | --- | --- | ---: |
| 等于计划最终分类 | `applied` | `succeeded` | 0 |
| 等于批准前分类 | `not_applied` | `failed` | 0 |
| 与两者均不同 | `conflict` | `failed` | 0 |
| Graph GET 失败 | `read_failed` | 保持原不确定状态 | 0 |

`not_applied` 或 `conflict` 进入 `failed` 后虽然可以重新认领，但步骤三执行器仍会重新读取并比较旧快照。若期间状态发生变化，它只会再次产生零写请求冲突，不会盲目覆盖。

对账器不通过 `changeKey` 单独推断 PATCH 是否发生，因为其他邮件属性变化也会更新 `changeKey`。最终判断以完整分类集合为准，`changeKey` 继续由后续执行写前检查使用。

## 离线验收

```powershell
uv run pytest tests/test_action_graph_executor.py tests/test_action_audit.py -q
```

测试覆盖：

- 执行成功产生 `executing -> write_in_flight -> succeeded` 状态审计；
- Graph 操作事件记录准确读写计数；
- 日志中不出现原始 Message ID；
- 认领后审计失败时 Graph 请求数为零；
- `write_in_flight` 和 `outcome_unknown` 均能对账；
- 已应用、未应用、第三状态冲突和读取失败四个分支；
- 每次对账 Graph PATCH 数量固定为零；
- 对账状态迁移和操作结论均进入追加式日志。

## 尚未实现

- 已由后半步骤五提供默认关闭的单动作执行和只读对账 CLI，但尚未完成真实 Outlook 单封验收；
- 没有批量执行入口和单次批量上限；
- 没有真实 Outlook 小批量验收；
- 没有自动日志轮换或跨文件事务；
- 没有执行真实回滚 PATCH。

单动作 CLI 的确认门、命令和退出码见[单动作执行与对账 CLI](stage3_single_action_cli.md)。下一步应先完成 no-write 与单封测试邮箱验收，再考虑小批量执行。
