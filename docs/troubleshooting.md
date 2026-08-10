# 故障排查

先运行以下只读命令：

```powershell
.\scripts\Start-InboxPilot.ps1 -Mode Doctor
uv run inbox-agent stats
```

脱敏结构化日志位于 `data/private/logs/inbox-pilot.jsonl`。日志不应包含 API Key、Token、邮件主题、正文、预览或原始 Message ID。

## 端口 8765 已被占用

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen
Get-Process -Id (Get-NetTCPConnection -LocalPort 8765 -State Listen).OwningProcess
```

先确认进程确实属于 InboxPilot。若网页仍可打开，从左侧使用“完全退出”；若另有独立 `service start`，还需在对应终端按 `Ctrl+C`。不要直接结束身份不明的进程。也可临时使用其他端口：

```powershell
.\scripts\Start-InboxPilot.ps1 -Mode Web -Port 8766
```

## 网页能打开但没有新邮件

检查“运行状态”页中的自动同步是否已启动，并确认 `config/service.local.yaml`：

- `workflow.sync_outlook: true`
- `workflow.graph_config_path: config/graph.local.yaml`
- `interval_minutes` 是期望的轮询间隔

然后运行一次：

```powershell
.\scripts\Start-InboxPilot.ps1 -Mode RunOnce
```

首次发现新邮件的最长正常等待时间约为一个同步周期，再加当前工作流处理耗时。

## Graph 登录或同步失败

确认 Client ID、租户模式、重定向设置和 `Mail.Read` 委托权限。重新执行：

```powershell
uv run inbox-agent outlook login --config config/graph.local.yaml
uv run inbox-agent outlook sync --config config/graph.local.yaml
```

只读与写入使用不同配置和 Token 缓存，不要混用。组织租户可能要求管理员同意；详细设置见 `docs/microsoft_graph_sync.md`。

## LLM 没有启用

LLM 默认关闭。Web 设置页中的 API Key 只存在于当前进程内存，Web 重启后需重新设置。确认 Provider、模型和密钥匹配，并检查 Provider 的 HTTPS 可达性。规则分析不依赖 LLM，Provider 失败不应阻止确定性规则路径。

## Doctor 返回退出码 2

退出码 2 表示存在警告，不一定表示服务不可运行。读取 JSON 或表格中的具体检查项，例如尚无备份、可选配置缺失或最近运行失败。错误级检查会返回退出码 1。

## 数据库版本或完整性异常

停止全部进程并先保留当前文件副本。运行：

```powershell
uv run inbox-agent db status --format json
uv run inbox-agent doctor --format json
```

正常升级使用 `uv run inbox-agent db init`。不要手工修改 Alembic Revision。若需要恢复，选择带 Manifest 的已验证备份并使用显式 `--confirm`；恢复命令还会在覆盖前再创建一份备份。

## 自动同步重复运行或锁冲突

同一配置只允许运行一个调度器。不要同时启动独立 `service start` 和网页管理的自动同步。状态页或 `uv run inbox-agent service status --config config/service.local.yaml` 会显示实际锁及持久化状态。

## Windows 通知未出现

确认 `notifications.enabled` 与 `desktop_enabled` 均为 `true`，并检查 Windows 通知权限和专注助手。远程会话、无桌面会话或 Docker 容器可能无法展示原生通知；每日摘要仍会写入私有目录。

## 后台启动后立即退出

查看：

- `data/private/logs/web.stdout.log`
- `data/private/logs/web.stderr.log`

常见原因是端口占用、依赖未同步或缺少 `config/service.local.yaml`。重新运行安装脚本不会覆盖已有私有配置。

## 仍无法定位

保存以下脱敏结果用于 Issue：

```powershell
uv run inbox-agent doctor --format json
uv run inbox-agent stats --format json
git rev-parse --short HEAD
uv --version
```

提交前再次检查输出，不要上传 `.env`、`config/*.local.yaml`、数据库、Token 缓存、邮件数据、私有日志或备份。

