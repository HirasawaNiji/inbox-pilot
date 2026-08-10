# Windows 个人电脑部署

本指南面向单用户 Windows 10/11 电脑。推荐原生 PowerShell 部署；Docker Compose 只是可选的隔离运行方式。

## 安全默认值

- Web 仅监听 `127.0.0.1`，不向局域网公开。
- 初始配置完全离线：`sync_outlook: false`、LLM 关闭、Outlook 写回关闭。
- 安装脚本不会覆盖已有 `.env` 或 `config/*.local.yaml`。
- 邮件、Token 缓存、API Key、SQLite、队列、日志和备份均位于 Git 忽略路径。
- 自动工作流只同步、分析并生成待确认动作，绝不自动写回 Outlook。

## 前置条件

安装 Git 和 [uv](https://docs.astral.sh/uv/)。项目要求 Python 3.12 或更高版本，uv 可以自动管理解释器和虚拟环境。

## 首次安装

```powershell
git clone https://github.com/HirasawaNiji/inbox-pilot.git
Set-Location inbox-pilot
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\Install-InboxPilot.ps1
```

安装脚本会执行锁定依赖同步、生成缺失的私有配置、建立私有目录、升级数据库并运行只读诊断。若环境已同步，可使用 `-SkipDependencySync`。

随后编辑 `config/graph.local.yaml`，填写 Entra 应用的 Client ID，并完成只读授权：

```powershell
uv run inbox-agent outlook login --config config/graph.local.yaml
uv run inbox-agent outlook sync --config config/graph.local.yaml
```

确认只读同步成功后，将 `config/service.local.yaml` 中的 `workflow.sync_outlook` 改为 `true`。不要因此启用写权限；写回仍使用独立的 `config/graph_write.local.yaml` 和人工确认门。

## 启动和退出

前台启动 Web：

```powershell
.\scripts\Start-InboxPilot.ps1 -Mode Web
```

后台最小化启动 Web：

```powershell
.\scripts\Start-InboxPilot.ps1 -Mode Web -Background
```

打开 <http://127.0.0.1:8765/console>。关闭浏览器只是“转入后台”，Web 与网页启动的自动同步仍继续运行。需要释放端口时，在页面中使用“完全退出”并输入 `EXIT`；该动作先停止网页管理的调度器，再优雅关闭 Web 进程。

也可以单独运行调度器：

```powershell
.\scripts\Start-InboxPilot.ps1 -Mode Service
```

此模式与 Web 是两个独立进程，必须在其终端按 `Ctrl+C` 单独停止。若希望网站与同步保持同一生命周期，推荐只启动 Web，再从“运行状态”页面启动自动同步。

其他常用模式：

```powershell
.\scripts\Start-InboxPilot.ps1 -Mode RunOnce
.\scripts\Start-InboxPilot.ps1 -Mode Doctor
```

## LLM（可选）

LLM 默认关闭。可在 Web“设置”页临时选择 OpenAI 或 DeepSeek、模型和 API Key。密钥只进入当前 Web 进程内存，进程退出即清除。也可以在私有 `.env` 中设置 `OPENAI_API_KEY` 或 `DEEPSEEK_API_KEY`，但不要提交该文件。

## 数据与备份目录

| 路径 | 内容 |
| --- | --- |
| `data/private/inbox_pilot.sqlite3` | 邮件元数据、分析、动作、运行状态与可观测性事件 |
| `data/private/audit/` | 私有动作审计日志 |
| `data/private/logs/` | 脱敏 JSONL 和后台 Web 输出 |
| `data/private/summaries/` | 不含完整正文的每日摘要 |
| `data/private/backups/` | SQLite 备份和 SHA-256 Manifest |
| `config/*.local.yaml` | 私有 Graph、LLM、写回和服务配置 |
| `.env` | 可选 API Key 环境变量 |

创建备份：

```powershell
uv run inbox-agent backup --format json
```

恢复前必须完全停止 Web 和独立调度器，再显式确认：

```powershell
uv run inbox-agent restore PATH_TO_BACKUP --confirm --format json
```

## 更新

先完全退出所有 InboxPilot 进程并备份数据库，然后：

```powershell
git pull --ff-only
uv sync --locked
uv run inbox-agent db init --format json
uv run inbox-agent doctor
```

## 可选 Docker Compose

先运行安装脚本生成 `.env`、私有配置和数据目录，再执行：

```powershell
docker compose up --build
```

Compose 仅将端口发布到主机的 `127.0.0.1:8765`，并挂载私有数据目录。容器内使用 Uvicorn，因此网页“完全退出”不可代替容器生命周期管理；完整退出请执行：

```powershell
docker compose down
```

Windows 原生桌面通知和基于 OS 的 Graph Token 缓存在容器中可能不可用，因此真实个人邮箱长期运行优先使用原生 PowerShell 部署。不要把 Compose 端口映射改成 `0.0.0.0`。

