# 阶段三步骤五：幂等、并发保护与安全重试

步骤五为未来的 Outlook 分类写入器建立本地执行安全边界。它不申请 `Mail.ReadWrite`，不构造 Graph 写请求，也不会修改真实邮箱。当前 Microsoft Graph 配置仍只允许 `Mail.Read`。

## 稳定幂等键

`actions build` 现在会为每个 `MailboxAction` 生成 64 位十六进制 SHA-256 幂等键。哈希输入包含：

- 邮件 ID；
- 动作类型，目前只能是 `set_categories`；
- 创建动作时观察到的全部 Outlook 分类；
- 可选的 Graph `changeKey`；
- 计划写入的 `InboxPilot/` 分类；
- 规则策略版本。

这些字段共同定义一次分类变更。输入完全相同时，重复构建会得到相同幂等键；任何快照、目标分类或策略版本变化都会得到新键。加载 `MailboxAction` 时会重新计算并校验该键，手工篡改动作内容而不更新键会被 Pydantic Schema 拒绝。

幂等键不等同于密码或访问令牌，但它来自私有邮件标识和分类元数据，仍应与动作队列一起保存在 `data/private/`，不要上传到 GitHub。

## 队列与审计文件锁

队列 JSON 和审计 JSONL 都使用同目录下的持久化 `.lock` 文件：

```text
data/private/action_queue.json.lock
data/private/audit/actions.jsonl.lock
```

Windows 使用 `msvcrt.locking`，POSIX 系统使用 `fcntl.flock`。锁是操作系统级的 advisory lock，默认最多等待 5 秒；超时后操作会失败，而不是绕过锁继续读写。

锁覆盖完整的“读取—校验—修改—写入”临界区，因此两个 CLI 或进程同时追加动作时不会发生最后写入者覆盖前一个更新的问题。审计日志的去重检查与追加也在同一个锁内完成。队列仍采用临时文件原子替换，审计日志仍采用追加、flush 和 `fsync`。

`.lock` 文件会保留在磁盘上，这是正常现象；真正的占用状态由操作系统管理。不要通过删除锁文件来处理正在运行的进程。

## 原子执行认领

未来的 Graph 写入器不能直接把动作从 `approved` 改为 `executing`，必须调用 `ActionQueueRepository.claim_execution()`。调用者需要同时提供动作 ID 和动作自身的幂等键。

认领结果分为三类：

| 结果 | 含义 | 是否应发送 Graph 写请求 |
| --- | --- | --- |
| `claimed` | 首次从 `approved` 原子进入 `executing` | 是 |
| `retry_claimed` | 从 `failed` 重试，或恢复陈旧的 `executing` | 是 |
| `already_succeeded` | 相同动作已经成功 | 否，必须安全 no-op |

若动作尚未批准、已拒绝、已回滚、缺少幂等键或调用者给出的键不匹配，认领会失败。若另一执行者已经持有尚未过期的 `executing` 状态，也会失败。这样可以阻止两个执行者同时处理同一动作。

开发者调用模式如下：

```python
claim = repository.claim_execution(action.action_id, action.idempotency_key)
if not claim.should_execute:
    return

try:
    # 后半阶段才会在这里执行受 allowlist 约束的 Graph 分类更新。
    execute_category_write(claim.action)
except Exception as error:
    repository.fail_execution(
        action.action_id,
        action.idempotency_key,
        note=str(error),
    )
    raise
else:
    repository.complete_execution(action.action_id, action.idempotency_key)
```

这段早期示例中的 `execute_category_write` 已由后半步骤三的 `ApprovedActionGraphExecutor` 取代，但项目仍未提供真实执行 CLI。

## 失败重试与陈旧执行恢复

正常失败必须调用 `fail_execution()`，将状态从 `executing` 改为 `failed` 并记录非空原因。之后再次认领会原子进入 `executing`，`attempt_number` 随认领次数递增。

受控执行器现在保证 `executing` 只覆盖 Graph PATCH 之前的写前检查。进程在此阶段崩溃时，动作可能停留在 `executing`，调用者可显式传入 `stale_after_seconds`：

```python
claim = repository.claim_execution(
    action.action_id,
    action.idempotency_key,
    stale_after_seconds=300,
)
```

只有当前租约年龄达到阈值时，仓储才会先记录一条带原因的 `failed` 迁移，再原子认领为新的 `executing` 尝试。未传阈值时不会自动夺取执行权；负数阈值会被拒绝。执行器在 PATCH 前会先持久化 `write_in_flight`；该状态和 `outcome_unknown` 都不能通过陈旧租约恢复，避免请求可能已生效时盲目重试。

当前唯一动作 `set_categories` 计划的是同一个目标分类集合，但幂等目标不等于可以盲目重发。后半步骤三已经实现写前重读、`changeKey` 和分类比较、非 `InboxPilot/` 分类保留及严格 URL allowlist，详见[受控执行器说明](stage3_preflight_executor.md)。

## 测试覆盖

自动化测试验证了：

- 同一动作在不同分析时间下仍产生稳定幂等键；
- 内容与幂等键不一致时模型加载失败；
- 首次认领、失败、重试、成功和成功后 no-op 的完整链路；
- 错误键、未批准动作和重复活跃认领被拒绝；
- 陈旧执行会留下失败原因并增加尝试次数；
- 文件锁超时后不会继续访问共享状态；
- 两个并发写入者不会丢失队列动作或审计事件。

## 当前限制与下一步

- 队列 JSON 与审计 JSONL 是两个文件，无法提供跨文件数据库事务；确定性审计事件可在后续命令中补齐；
- advisory lock 要求所有写入者都遵守仓储接口，外部程序直接改文件不受保护；
- 尚未实现执行租约所有者 ID 或心跳，恢复策略仅基于最后状态时间与显式阈值；
- 已实现默认流程之外的底层 Graph 分类客户端、写前重读执行器、单动作真实入口和只读对账；尚未实现真实回滚执行和小批量验收。

步骤六已实现受控回滚计划并完成阶段三前半离线验收，详见[回滚说明](stage3_rollback.md)和[验收报告](stage3_front_half_acceptance.md)。
