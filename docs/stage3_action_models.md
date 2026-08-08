# 阶段三步骤一：邮箱动作模型与状态机

阶段三首先建立本地、不可变、可审计的动作契约。此步骤不调用 Microsoft Graph 写接口，不申请 `Mail.ReadWrite`，也不修改真实邮箱。

## 模型组成

- `OutlookCategorySnapshot`：保存生成动作时观察到的 Outlook 分类、时间和可选 change key；
- `CategoryWritePlan`：只描述 InboxPilot 管理的目标分类，并强制保留用户的非 InboxPilot 分类；
- `ActionEvidence`：同时保存规则评估、可选 LLM 原始分析和最终融合结果；
- `ActionTransition`：记录一次经过白名单校验的状态变化、发生时间、行为主体和说明；
- `MailboxAction`：聚合动作 ID、邮件 ID、快照、计划、证据、状态和完整迁移历史。

当前唯一允许的动作类型是：

```text
set_categories
```

移动、删除、发送、修改正文和标记已读均不在模型枚举中，因而不能被后续执行器静默启用。

## 分类安全边界

动作计划只能包含 `InboxPilot/` 前缀：

```text
InboxPilot/P1
InboxPilot/security_alert
InboxPilot/review
```

计划必须与最终 `TriageResult` 的优先级、类别和 `requires_review` 完全一致。`preserve_unmanaged_categories` 固定为 `true`，后续计划生成器和 Graph 执行器都必须保留用户已有的 `School`、`Important` 等分类。

## 状态机

允许的状态迁移为：

```text
pending_review -> approved -> executing -> succeeded -> rolled_back
       |             |          |
       +-> rejected  +-> rejected
                                +-> failed -> executing
                                +-> write_in_flight -> succeeded
                                          |          +-> failed
                                          +-> outcome_unknown -> succeeded / failed
```

约束如下：

- `approved` 和 `rejected` 只能由 `user` 触发；
- `executing`、`write_in_flight`、`outcome_unknown`、`succeeded`、`failed` 和 `rolled_back` 只能由 `system` 触发；其中 `rolled_back` 只能表示执行器已成功完成真实恢复，用户提出回滚意图不能提前改变状态；
- `failed`、`outcome_unknown` 和 `rolled_back` 必须包含说明；
- `rejected` 与 `rolled_back` 是终止状态；
- 失败动作可以重新进入 `executing`，为后续安全重试预留通道；
- `write_in_flight` 和 `outcome_unknown` 不能重新认领，必须先重读邮箱完成对账；
- 历史记录必须从 `pending_review` 开始、状态连续且时间单调递增；
- 当前状态和 `updated_at` 必须与最后一条迁移一致。

## 证据一致性

- 动作、最终判断和 LLM 分析的邮件 ID 必须一致；
- `llm` 或 `hybrid` 决策必须携带 LLM 原始分析；
- 纯规则决策的最终优先级必须与规则评估一致；
- 分类快照不得晚于动作创建时间；
- 所有时间必须包含时区。

## 后续步骤

步骤二已在这些模型之上实现本地人工确认队列、原子 JSON 持久化和批准/拒绝 CLI。使用方法见[本地人工确认队列](stage3_review_queue.md)。当前仍保持 Graph `Mail.Read`，不会执行真实写回。
