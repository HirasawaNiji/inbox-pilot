# 阶段三后半步骤一：写权限与 Immutable ID 基础

本步骤只建立后续 Outlook 分类写回所需的权限与身份基础，不执行任何邮箱写请求。当前代码没有调用 `PATCH`、`POST`、`DELETE`，也不会修改邮件分类、移动邮件或发送邮件。

## 已实现的安全边界

### 1. 读取与写授权完全分离

- `config/graph.local.yaml` 仍只允许委托权限 `Mail.Read`；
- `config/graph_write.local.yaml` 只允许委托权限 `Mail.ReadWrite`；
- 写授权默认由 `write_enabled: false` 关闭；
- 写授权令牌固定保存在 `data/private/msal_write_token_cache.bin`，不会复用只读令牌缓存；
- 两个本地配置和两个令牌缓存均位于 Git 忽略范围内。

`Mail.Send`、应用程序权限以及 `MailboxSettings.ReadWrite` 均不在本步骤的允许范围。后续分类写回只应使用更新邮件 `categories` 属性所需的最小委托权限。

### 2. 写授权入口不执行邮箱写入

`outlook write-login` 只完成 MSAL 设备码授权和加密令牌缓存。命令成功时会明确输出：

```text
No Microsoft Graph mailbox write request was sent.
```

真实 Graph 写客户端尚未实现，因此即使完成授权，也不会自动修改任何邮件。

### 3. 邮件 ID 使用 Immutable ID

所有 Inbox Delta 请求都带有：

```http
Prefer: IdType="ImmutableId"
```

同步状态和同步报告使用 `message_id_type: restImmutableEntryId` 标记这一契约。这样后续人工确认队列中的 `source_id` 才能作为写回目标，降低邮件在同一邮箱内移动后 ID 改变造成误写的风险。

旧版状态文件缺少该字段时仍可兼容读取；下一次完整同步会写入明确标记。来自未知版本或自定义 Graph 客户端的数据不能直接假定为 Immutable ID，应重新进行受控同步后再允许写回。

## 本地准备步骤

### 第一步：检查 Entra 应用

在现有公共客户端应用中添加 Microsoft Graph 的“委托的权限” `Mail.ReadWrite`。不要创建 Client Secret，不要选择“应用程序权限”，也不要添加 `Mail.Send`。

只读同步仍可继续使用原来的 `Mail.Read` 配置和缓存。写权限授权使用单独配置，便于随时关闭或撤销。

### 第二步：创建私有写配置

```powershell
Copy-Item config/graph_write.example.yaml config/graph_write.local.yaml
```

打开 `config/graph_write.local.yaml`：

1. 将 `client_id` 改为自己的应用 Client ID；
2. 确认 `scopes` 只有 `Mail.ReadWrite`；
3. 完成权限审查后，将 `write_enabled` 从 `false` 改为 `true`；
4. 不要修改固定的 `token_cache_path`。

### 第三步：只执行写权限授权

```powershell
uv run inbox-agent outlook write-login --config config/graph_write.local.yaml
```

在浏览器设备码页面确认账号和权限。成功后只会创建或更新本地加密写令牌缓存，不会修改邮箱。

### 第四步：确认只读同步仍正常

```powershell
uv run inbox-agent outlook sync --config config/graph.local.yaml --format json
```

输出中应包含：

```json
"message_id_type": "restImmutableEntryId"
```

## 本步骤的离线验收

```powershell
uv run pytest tests/test_graph_config.py tests/test_graph_auth.py tests/test_graph_client.py tests/test_graph_sync.py tests/test_cli.py -q
```

测试覆盖以下约束：

- 写权限默认关闭，关闭时不会创建 Token Provider；
- 只读配置拒绝 `Mail.ReadWrite`，写配置拒绝 `Mail.Read` 和其他额外权限；
- 写令牌不能复用只读缓存路径；
- 写授权命令不会输出访问令牌，并明确声明写请求数为零；
- Delta 请求始终声明 Immutable ID，同步状态和报告持久化 ID 类型。

## 后续步骤

严格 allowlist 的 Graph 分类写客户端和写前重读执行器现已完成，详见[阶段三后半步骤二](stage3_graph_category_write_client.md)与[步骤三](stage3_preflight_executor.md)。下一步是执行审计、结果未知对账和默认关闭的真实入口；这些边界完成前仍不应进行真实邮箱写入验收。

## 官方参考

- [更新邮件资源（Microsoft Graph）](https://learn.microsoft.com/en-us/graph/api/message-update?view=graph-rest-1.0)
- [Mail.ReadWrite 权限说明](https://learn.microsoft.com/en-us/graph/permissions-reference#mailreadwrite)
- [Outlook Immutable ID](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)
