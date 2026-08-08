# Microsoft Graph 个人 Outlook 只读同步指南

本文说明如何让 InboxPilot 使用个人 Microsoft 账户读取 Outlook 收件箱。当前实现采用 Microsoft Graph 委托权限、设备代码登录和 `Mail.Read` 最小权限，只发送 `GET` 请求，不会移动、删除、标记或修改邮件。

项目维护者已于 2026-08-08 使用个人 Outlook 完成设备码登录、首次分页同步、第二次 Delta 增量同步、新邮件同步和本地 Pipeline 分析验收。本指南供每位使用者连接自己的账户；不要复用他人的 Client ID、令牌缓存或真实邮件数据。

## 1. 方案边界

- 账户：个人 Outlook / Hotmail / Live Microsoft 账户；
- 登录：用户本人在微软页面完成设备代码登录和授权；
- 权限：仅 `Mail.Read`；
- 范围：仅同步 Inbox；
- 存储：令牌缓存、同步游标和邮件数据只保存在 `data/private/`；
- 增量：首次读取最近一段时间，后续使用 Microsoft Graph Delta Link 获取变化；
- 附件：只记录是否存在附件，不下载附件内容；
- 写回：当前版本没有 Outlook 分类、移动、删除或已读状态写回能力。

令牌缓存由操作系统安全存储加密；同步后的邮件数据集是 Git 忽略的普通 JSON，并未额外加密。请只在受信任的本地设备上使用。

QQ 邮箱不能替代 Outlook 来测试 Microsoft Graph 邮件接口；此方案需要个人 Microsoft 账户。Azure 免费账户的 30 天额度并不是此功能的运行时限：应用注册和委托登录本身不需要创建收费的 Azure 计算资源。

## 2. 注册个人账户应用

1. 使用准备测试的个人 Microsoft 账户登录 [Microsoft Entra 管理中心](https://entra.microsoft.com/)。
2. 进入“应用注册”，选择“新注册”。
3. 输入应用名称，例如 `InboxPilot Local Development`。
4. “支持的账户类型”选择包含个人 Microsoft 账户的选项。若只测试个人 Outlook，可选择“仅个人 Microsoft 账户”。
5. 本地设备代码登录不需要添加 Web 重定向 URI，完成注册。
6. 在应用概览中复制“应用程序（客户端）ID”。这是公开标识，不是 Client Secret。
7. 在“身份验证”中允许公共客户端流（Allow public client flows）。
8. 在“API 权限”中添加 Microsoft Graph 的“委托的权限” `Mail.Read`。
9. 删除项目不需要的其他邮件权限。不要添加 `Mail.ReadWrite`、`Mail.Send` 或应用程序权限。

本项目是公共客户端，不需要也不应创建 Client Secret。个人账号授权通常由当前用户在首次登录时同意；若门户策略要求管理员同意，需要按该租户实际策略处理。

## 3. 创建本地配置

先安装锁定版本的依赖：

```powershell
Set-Location inbox-pilot
uv sync --locked
```

复制公开模板：

```powershell
Copy-Item config/graph.example.yaml config/graph.local.yaml
```

编辑 `config/graph.local.yaml`：

```yaml
client_id: "替换为应用程序客户端 ID"
account_audience: consumers
scopes:
  - Mail.Read
mail_folder: inbox
initial_sync_days: 30
page_size: 50
request_timeout_seconds: 30
token_cache_path: data/private/msal_token_cache.bin
sync_state_path: data/private/graph_sync_state.json
dataset_path: data/private/outlook_inbox.json
```

字段含义：

| 字段 | 含义 |
| --- | --- |
| `client_id` | Entra 应用的客户端 ID，必须是 UUID |
| `account_audience` | `consumers` 仅个人账户；也支持 `organizations` 或 `common` |
| `scopes` | 固定为唯一值 `Mail.Read`，配置其他权限会被拒绝 |
| `mail_folder` | 固定为 `inbox` |
| `initial_sync_days` | 首次同步向前读取的天数，允许 1～365 |
| `page_size` | 每页请求数量，允许 1～100 |
| `request_timeout_seconds` | 单次 Graph 请求超时秒数 |
| `token_cache_path` | OS 加密的 MSAL 令牌缓存 |
| `sync_state_path` | Delta Link 增量同步状态 |
| `dataset_path` | 转换为 InboxPilot Schema 后的本地邮件数据集 |

三个路径都必须是 `data/private/` 下的相对路径。`config/*.local.yaml` 和 `data/private/` 已列入 `.gitignore`，不会被正常的 Git 添加操作提交。

## 4. 登录和同步

首次登录：

```powershell
uv run inbox-agent outlook login --config config/graph.local.yaml
```

终端会显示微软登录网址和一次性设备代码。在浏览器中完成登录和 `Mail.Read` 授权。不要把访问令牌、设备代码或令牌缓存发给他人。

同步收件箱：

```powershell
uv run inbox-agent outlook sync --config config/graph.local.yaml
```

输出机器可读报告：

```powershell
uv run inbox-agent outlook sync --config config/graph.local.yaml --format json
```

首次同步成功时，报告中的 `started_from_delta` 应为 `false`、`completed` 应为 `true`，并且 `failures` 应为空。随后再次运行相同命令，`started_from_delta` 应变为 `true`，证明保存的 Delta Link 已用于增量同步。

推荐再向自己的 Outlook 发送一封不含真实隐私的测试邮件，然后执行第三次同步；报告中的 `created_count` 应增加。

同步成功后，可以把私有数据集交给现有分析流水线：

```powershell
uv run inbox-agent analyze data/private/outlook_inbox.json
```

如需启用 OpenAI 或 DeepSeek，再显式传入本地 LLM 配置；Outlook 同步本身不需要模型 API Key。

## 5. 增量同步行为

首次运行从最近 `initial_sync_days` 天开始分页读取。Graph 返回最终 Delta Link 后，项目将它写入私有状态文件。后续运行从该链接继续，只处理新增、更新和移除变化。

只有完整取得最终 Delta Link 且所有返回邮件都成功转换时，才会推进同步状态。如果某封邮件转换失败，已成功转换的数据仍会安全写入，但旧 Delta Link 会保留，使失败项可以在下次同步时重试。

Graph 邮件 ID 使用 Immutable ID 首选项，降低邮件在文件夹中移动后 ID 改变造成重复记录的风险。

## 6. 安全检查

提交代码前运行：

```powershell
git status --short
git check-ignore -v config/graph.local.yaml
git check-ignore -v data/private/msal_token_cache.bin
git check-ignore -v data/private/graph_sync_state.json
git check-ignore -v data/private/outlook_inbox.json
```

确认下列内容没有出现在提交中：

- `config/graph.local.yaml`；
- `data/private/` 中的令牌、状态和真实邮件；
- 访问令牌、刷新令牌、设备代码；
- 真实邮件正文、姓名、学号或内部链接。

Client ID 不是密码，但仍建议只放在被忽略的本地配置里，避免公开仓库与个人测试应用产生不必要的绑定。

## 7. 重新授权与清理

若需要切换账户或重新授权，先在 Microsoft 账户的应用授权页面撤销 InboxPilot 的访问，再删除本地 `data/private/msal_token_cache.bin`，随后重新执行 `outlook login`。

若要完全清除本地同步数据，可删除 `data/private/` 下本项目生成的令牌缓存、同步状态和邮件数据集。这些文件不会影响 Outlook 云端邮件，因为当前实现没有删除或写回接口。

## 8. 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 配置提示 Client ID 无效 | 确认复制的是“应用程序（客户端）ID”，格式为 UUID |
| 个人账号无法登录 | 检查账户类型是否支持个人 Microsoft 账户，并使用 `consumers` |
| 提示 public client 不允许 | 在应用“身份验证”页面启用公共客户端流 |
| 提示权限或同意失败 | 确认添加的是委托权限 `Mail.Read`，不是应用程序权限 |
| `sync` 提示没有缓存账号 | 先运行一次 `outlook login` |
| Graph 返回 429 | CLI 会根据 `Retry-After` 报告限流，请稍后重试 |
| 本地令牌缓存不可用 | 检查当前操作系统的安全存储是否可用，并查看 CLI 错误信息 |

## 9. 验收清单

- [ ] 使用真实个人 Outlook 应用 Client ID 创建本地配置；
- [ ] 设备代码登录成功，邮件权限包含 `Mail.Read` 且不包含读写或发送权限；
- [ ] 首次同步生成私有数据集和 Delta 状态；
- [ ] 第二次同步显示增量模式；
- [ ] 新测试邮件可由后续增量同步获取；
- [ ] `analyze data/private/outlook_inbox.json` 可以处理同步结果；
- [ ] `git status` 中没有本地配置、令牌、状态或真实邮件。

实现使用的微软官方参考：

- [MSAL 设备代码流](https://learn.microsoft.com/en-us/entra/msal/msal-authentication-flows)
- [Microsoft Graph Mail.Read 权限](https://learn.microsoft.com/en-us/graph/permissions-reference)
- [邮件 Delta Query](https://learn.microsoft.com/en-us/graph/delta-query-messages)
- [Outlook Immutable ID](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)
