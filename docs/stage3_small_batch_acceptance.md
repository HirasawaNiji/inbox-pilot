# 阶段三：小批量真实 Outlook 验收

本验收用于证明 InboxPilot 能在连续处理少量邮件时继续遵守单动作确认、写前重读、用户分类保留、
一次最多一个 PATCH、追加式审计和可回滚边界。它不是批量写入功能，也不会新增绕过确认门的入口。

## 验收规模与原则

- 固定使用 3 封可丢弃的专用测试邮件；
- 三封邮件必须具有唯一主题，不能使用真实重要邮件；
- 每封邮件单独生成动作、人工批准、dry-run、确认和执行；
- 前一封在 Outlook 中人工确认通过后，才能处理下一封；
- 任意一封出现冲突、失败、结果未知、分类不符或内容变化，立即停止剩余动作；
- 不并发执行，不使用脚本循环调用 `actions execute`，不增加批量 PATCH API；
- 测试数据、动作队列、审计日志和 Graph ID 全部保存在 `data/private/acceptance-batch/`，不进入 Git。

## 三封专用测试邮件

建议继续从测试用 QQ 邮箱发送到已连接的个人 Outlook 邮箱。

### 邮件 001：紧急学业截止时间

主题：

```text
InboxPilot-Batch-Acceptance-20260809-001
```

正文：

```text
【紧急】测试作业尚未提交，请在 2026 年 8 月 10 日 20:00 前提交。
这是 InboxPilot 小批量验收邮件，仅用于分类测试。
```

收到后在 Outlook 中手工添加非托管分类：

```text
BatchKeep-001
```

### 邮件 002：普通课程通知

主题：

```text
InboxPilot-Batch-Acceptance-20260809-002
```

正文：

```text
下周课程阅读材料已经发布，无需提交，无需回复。
这是 InboxPilot 小批量验收邮件，仅用于分类测试。
```

收到后添加：

```text
BatchKeep-002
```

### 邮件 003：低优先级活动简报

主题：

```text
InboxPilot-Batch-Acceptance-20260809-003
```

正文：

```text
校园社团活动简报与优惠信息，无需报名，自愿参加，无需回复。
这是 InboxPilot 小批量验收邮件，仅用于分类测试。
```

收到后添加：

```text
BatchKeep-003
```

实际优先级和类别由当前规则或 LLM 结果决定，不以预先猜测的 P1/P3/P5 作为通过条件。验收关注的是
动作计划、Outlook 写入结果和安全边界是否一致。

## 隔离文件

本次验收使用以下私有路径：

```text
data/private/acceptance-batch/messages.json
data/private/acceptance-batch/action_queue.json
data/private/acceptance-batch/actions.jsonl
data/private/acceptance-batch/acceptance_results.json
```

不要复用单封验收的 `data/private/acceptance/action_queue-v2.json`，因为该动作已经进入
`rolled_back` 终态。

## 准备流程

1. 按模板发送三封邮件；
2. 在 Outlook 中分别添加 `BatchKeep-001`、`BatchKeep-002`、`BatchKeep-003`；
3. 运行只读同步，确保三封邮件和最新 `changeKey` 已进入 `outlook_inbox.json`；
4. 按精确主题筛选三封邮件，生成隔离的 `messages.json`；
5. 严格校验筛选结果恰好为 3 封、Graph Immutable ID 唯一、三项 `changeKey` 均非空；
6. 使用隔离路径生成动作队列，并核对每封动作均保留对应 `BatchKeep-*` 分类；
7. 在任何真实写入前保存队列、审计日志和三封邮件的本地快照。

三封邮件准备完成后再执行同步和筛选，避免对现有收件箱进行模糊匹配。

## 逐封执行顺序

每一封都按以下顺序处理，不得跳步：

```text
show -> approve -> apply --dry-run -> execute --confirm-action -> Outlook 人工检查
```

人工检查内容：

- 对应 `BatchKeep-00N` 分类仍然存在；
- 实际 InboxPilot 分类与 dry-run 计划完全一致；
- 其他两封尚未执行的测试邮件没有变化；
- 邮件没有被移动、删除或发送；
- 主题、正文、发件人和附件状态没有变化；
- 队列状态为 `succeeded`，审计中该动作记录一次 GET、最多一次 PATCH。

只有全部检查通过，才能继续下一封。

## 立即停止条件

遇到以下任一情况，停止剩余验收且不要盲目重试：

- `conflict`：重新同步并调查分类或 `changeKey` 变化；
- `outcome_unknown`：仅运行 `actions reconcile`，不得再次执行 PATCH；
- `failed`：保存输出和审计记录，确认 Graph 是否明确拒绝；
- 用户分类丢失、非目标邮件变化、主题或正文变化；
- 单个动作的 Graph PATCH 计数超过 1；
- 队列、回滚快照或审计日志无法持久化。

## 清理与回滚

三封正向写入全部通过后，使用每个动作自己的 rollback dry-run、回滚幂等键和确认门逐封恢复。
回滚仍必须一次处理一封，并在 Outlook 中确认 `BatchKeep-00N` 得到保留、InboxPilot 分类恢复到
动作创建前状态。清理完成后可以在 Outlook 中手工删除三封专用测试邮件和三个 `BatchKeep-*`
分类；删除邮件不属于 InboxPilot 自动化验收动作。

## 通过条件

- 3/3 动作均与各自 dry-run 计划一致；
- 每封邮件的非托管分类均得到保留；
- 每个动作最多一次 GET 和一次 PATCH，无自动重试；
- 没有移动、删除、发送或内容改写；
- 三个动作具有独立幂等键、完整状态链和隐私受限审计；
- 逐封受控回滚和人工检查通过；
- `acceptance_results.json` 记录脱敏结果，不包含原始 Graph Message ID、正文或令牌。

## 真实验收记录

2026-08-09，个人 Outlook 中的三封专用测试邮件完成小批量验收：

- 三封邮件按顺序完成正向写入，分类计划分别为 P1 学业截止、P4 课程材料和 P5 校园活动；
- `BatchKeep-001`、`BatchKeep-002`、`BatchKeep-003` 在正向写入中全部得到保留；
- 每个正向动作均执行一次 GET、一次 PATCH，状态为 `succeeded`，没有自动重试；
- 用户确认三封邮件的 InboxPilot 分类与各自 dry-run 完全一致；
- 三封动作随后按顺序执行受控回滚，每封均为一次 GET、一次 PATCH；
- 三个终态均为 `rolled_back`，回滚目标分别只保留对应的 `BatchKeep-*`；
- 用户确认所有 InboxPilot 分类已移除，用户分类仍存在；
- 邮件没有被移动、删除或发送，主题、正文、发件人和附件状态均未变化；
- 没有其他非测试邮件被修改。

因此阶段三小批量真实 Outlook 验收正式通过。真实邮件、Graph ID、令牌、私有队列、回滚快照
和完整审计日志只保存在 `data/private/acceptance-batch/`，不进入 Git。
