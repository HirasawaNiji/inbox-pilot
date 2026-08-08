# 阶段三后半步骤二：Graph 分类写客户端

本步骤实现了严格 allowlist 的 Microsoft Graph 分类写客户端，但尚未把它接入人工确认队列、执行状态机或 CLI。因此项目的默认运行流程仍不会发送真实邮箱写请求，本步骤验收也全部使用 `httpx.MockTransport` 离线完成。

## 唯一允许的写操作

客户端只允许以下请求：

```http
PATCH https://graph.microsoft.com/v1.0/me/messages/{immutable-message-id}
Authorization: Bearer <delegated-token>
Content-Type: application/json
Prefer: IdType="ImmutableId"

{
  "categories": ["School", "InboxPilot/P2", "InboxPilot/course_notice"]
}
```

调用者不能传入 URL、HTTP 方法或任意 JSON 对象。客户端内部固定 Graph 主机、`v1.0`、`/me/messages/{id}`、`PATCH` 和 `categories` 字段，因此不能通过该接口修改主题、正文、收件人、已读状态、重要性，也不能移动、删除或发送邮件。

## 请求契约

`GraphCategoryWriteRequest` 包含三个字段：

- `message_id`：来自 Immutable ID Delta 同步的邮件 ID；
- `message_id_type`：固定为 `restImmutableEntryId`，不能切换为普通 REST ID；
- `categories`：最终完整分类集合，最多 103 项，每项最多 255 个字符，不能为空或忽略大小写后重复。

Message ID 被视为不透明值并使用 URL 编码，最终只能占据一个路径段。带控制字符或首尾空白的 ID 会在发送请求前被拒绝。

分类请求写入的是完整 `categories` 集合，而不是只发送新增分类。因此未来执行器必须使用已经验证过的 dry-run `final_categories`，并在写前重新读取邮件状态，确认用户自己的分类没有在人工批准后发生变化。

## 响应验证

Graph 必须返回 `200 OK` 和可验证的邮件对象。客户端要求响应至少包含：

- 与请求一致的 Immutable Message ID；
- 与请求一致的分类集合，比较时忽略顺序和大小写；
- 非空的新 `changeKey`。

只有三项全部满足时才返回 `GraphCategoryWriteResult`。响应中的主题、正文等其他属性不会进入结果模型。

## 错误分类与重试原则

| 情况 | 异常 | 当前含义 |
| --- | --- | --- |
| `401` / `403` | `GraphAuthorizationError` | 令牌或权限不满足，需要重新授权或停止执行 |
| `429` | `GraphThrottledError` | 记录 `Retry-After`，本客户端不会自行重试 |
| `409` / `412` | `GraphWriteConflictError` | 资源冲突，后续执行器应重新读取而不是盲目覆盖 |
| 其他 `4xx` / `5xx` | `GraphServiceError` | Graph 明确返回失败 |
| 网络中断、异常成功状态、无效成功响应或响应不一致 | `GraphWriteOutcomeUnknownError` | 请求可能已生效，禁止直接重复写入，必须先重读确认 |
| 任意 `3xx` | `GraphWriteRedirectRejectedError` | 禁止带令牌的写请求跳转到其他地址 |

客户端只尝试一次请求，不包含自动重试。幂等认领、限流等待、结果未知后的重读确认属于后续执行器步骤。

## 为什么本步骤没有发送 `If-Match`

Microsoft Graph 的 message update 文档列出了 `PATCH /me/messages/{id}`、`Mail.ReadWrite` 和可更新的 `categories` 属性，但没有正式声明该接口支持使用 message `changeKey` 作为 `If-Match` 的并发条件。

因此本项目不会假设一个未记录的乐观锁协议。后半步骤三的受控执行器会在写前重新读取并比较 `changeKey` 和分类：若任一字段改变，动作转入冲突处理，不发送 PATCH。

## 离线验收

```powershell
uv run pytest tests/test_graph_write_client.py -q
```

测试覆盖：

- 请求方法、固定端点、认证头和 Immutable ID 请求头；
- Message ID 路径转义，阻止路径和查询注入；
- 请求正文严格只有 `categories`；
- 默认关闭写权限时无法创建客户端；
- 禁止重定向，即使传入的 HTTP Client 默认允许跳转；
- 权限、限流、冲突、服务器错误和网络中断分类；
- 成功响应的 ID、分类和 `changeKey` 一致性验证；
- 错误信息不泄露访问令牌。

## 后续集成状态

- 没有真实写入 CLI；
- [x] 后半步骤三已实现从人工确认队列认领单个动作；
- [x] 后半步骤三已实现写前重读和陈旧快照冲突检查；
- [x] 后半步骤三已把成功、失败和结果未知状态写回队列；
- 尚未把同一次执行的状态事件追加到审计日志；
- 没有真实 Outlook 小批量验收；
- 没有执行回滚写入。

受控执行器的流程和防崩溃状态见[阶段三后半步骤三](stage3_preflight_executor.md)。下一步是补齐执行审计和结果未知对账；真实执行 CLI 完成前，不应通过临时代码直接调用本客户端写入真实邮箱。

## 官方参考

- [更新邮件资源](https://learn.microsoft.com/en-us/graph/api/message-update?view=graph-rest-1.0)
- [Outlook Immutable ID](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)
- [Microsoft Graph 错误响应和资源限制](https://learn.microsoft.com/en-us/graph/errors)
