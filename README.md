# InboxPilot

[![CI](https://github.com/HirasawaNiji/inbox-pilot/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/HirasawaNiji/inbox-pilot/actions/workflows/ci.yml)

InboxPilot 是一个面向 Microsoft 365 / Outlook 的本地、可解释邮件优先级 Agent。它持续读取新邮件，生成 P1～P5 优先级、类别、摘要、待办与截止时间，并通过本地 Web 控制台解释每一次判断。

默认情况下，InboxPilot 完全离线、LLM 关闭、Outlook 写回关闭。连接邮箱、调用模型和修改邮件分类都需要用户分别配置和授权。

## 核心能力

- **可解释分类**：YAML 规则、可选 LLM 结果与最终融合理由完整展示；
- **Outlook 增量同步**：通过 Microsoft Graph 委托权限只读同步个人收件箱；
- **本地 Web 控制台**：查看优先级、分类理由、人工复核队列和运行状态；
- **受控分类写回**：人工批准、零写入预览、精确确认、冲突检查和受控回滚；
- **主动提醒**：高优先级邮件、临近截止事项、工作流故障和每日摘要；
- **长期运行保障**：SQLite 持久化、单实例调度、结构化日志、诊断、备份和恢复。

## 快速开始

### 环境要求

- Windows 10/11
- [Git](https://git-scm.com/)
- [uv](https://docs.astral.sh/uv/)

Python 3.12 或更高版本可由 uv 自动管理。

### 1. 安装

```powershell
git clone https://github.com/HirasawaNiji/inbox-pilot.git
Set-Location inbox-pilot
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\Install-InboxPilot.ps1
```

安装脚本会同步锁定依赖、创建私有配置、初始化数据库并运行健康检查，不会覆盖已有本地配置。

### 2. 选择数据来源

尚未连接邮箱时，可以先运行匿名离线 Demo：

```powershell
uv run inbox-agent demo
```

要处理自己的邮件，请按照 [Microsoft 365 / Outlook 只读接入指南](docs/microsoft_graph_sync.md)完成应用注册、登录和首次同步。真实邮件、Token 和本地配置都保存在 Git 忽略路径中。

### 3. 启动控制台

```powershell
.\scripts\Start-InboxPilot.ps1 -Mode Web
```

打开 <http://127.0.0.1:8765/console>。在“运行状态”页面启动自动同步后，InboxPilot 会按照 `config/service.local.yaml` 中的时间间隔持续读取和分析新邮件。

需要关闭终端但继续运行时：

```powershell
.\scripts\Start-InboxPilot.ps1 -Mode Web -Background
```

关闭浏览器不会停止后台服务；需要释放端口时，请在控制台中使用“完全退出”。完整安装、更新、后台运行和数据目录说明见 [Windows 部署指南](docs/deployment_windows.md)。

## 可选能力

| 能力 | 默认状态 | 配置指南 |
| --- | --- | --- |
| OpenAI / DeepSeek 辅助分析 | 关闭 | [LLM Provider 接入](docs/llm_provider.md) |
| Outlook 只读增量同步 | 未授权 | [Microsoft Graph 同步](docs/microsoft_graph_sync.md) |
| Outlook 分类写回 | 关闭 | [写权限基础](docs/stage3_write_permission_foundation.md)、[单动作执行](docs/stage3_single_action_cli.md) |
| Docker Compose | 可选 | [Windows 部署指南](docs/deployment_windows.md#可选-docker-compose) |

LLM 也可以直接在 Web“设置”页面临时启用。API Key 只保存在当前 Web 进程内存中，进程退出后自动清除。

## 工作方式

```text
Outlook / JSON
      ↓
增量同步与标准化
      ↓
YAML 规则 ── 可选 LLM
      ↓          ↓
      保守融合与可解释结果
                 ↓
       人工复核与动作预览
                 ↓
        单封写回 / 对账 / 回滚
```

自动工作流只负责同步、分析和生成待确认动作，不会自动修改 Outlook。真实写回始终要求人工批准和二次确认。

## 安全与隐私

- Web 服务只监听 `127.0.0.1`，不向局域网公开；
- 邮件、Token、API Key、数据库、日志和本地配置不会提交到 Git；
- 只读同步与写权限使用独立配置、权限范围和加密 Token 缓存；
- 写回只允许修改单封邮件的 `categories`，不会移动、删除、发送或改写邮件；
- 写入前重新读取实时分类与 `changeKey`，不确定结果只允许只读对账；
- 日志不记录邮件正文、主题、原始 Message ID、Token 或 API Key。

更完整的安全设计见 [写权限基础](docs/stage3_write_permission_foundation.md)、[执行审计与对账](docs/stage3_execution_audit_and_reconciliation.md)和[受控回滚](docs/stage3_rollback.md)。

## 运维命令

```powershell
uv run inbox-agent doctor
uv run inbox-agent stats
uv run inbox-agent backup
```

恢复数据库属于危险操作，需要先完全停止 InboxPilot，再显式执行 `restore BACKUP --confirm`。详细说明见 [可观测性与故障恢复](docs/stage4_observability_recovery.md)和[故障排查](docs/troubleshooting.md)。

## 文档

| 主题 | 文档 |
| --- | --- |
| 安装、启动与更新 | [Windows 部署指南](docs/deployment_windows.md) |
| Outlook 只读同步 | [Microsoft Graph 同步](docs/microsoft_graph_sync.md) |
| OpenAI / DeepSeek | [LLM Provider](docs/llm_provider.md) |
| 规则与优先级 | [YAML 规则配置](docs/rules_configuration.md) |
| Web 控制台 | [Web 控制台指南](docs/stage4_web_console.md) |
| 分类写回与回滚 | [单动作执行](docs/stage3_single_action_cli.md)、[受控回滚](docs/stage3_rollback.md) |
| 日志、诊断与恢复 | [可观测性与故障恢复](docs/stage4_observability_recovery.md) |
| 常见问题 | [故障排查](docs/troubleshooting.md) |
| 开发进度与验收 | [项目状态](docs/project_status.md) |


## 项目状态

项目初期开发阶段已经结束，当前版本是一个功能闭环、可在个人电脑长期运行的完整 MVP。
详细开发阶段、真实 Outlook 验收、自动化质量基线和后续增强方向见 [项目状态与验收记录](docs/project_status.md)。

## License

本项目采用 [MIT License](LICENSE) 开源。
