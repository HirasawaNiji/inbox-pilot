# 阶段四步骤六：本地通知与每日摘要

步骤六让长期运行的 InboxPilot 在发现高优先级邮件、可靠截止事项或工作流故障时主动提醒用户，
并每天生成一份位于私有目录的 Markdown 摘要。通知层运行在既有 `ServiceRunner` 完成一次工作流之后，
因此从 CLI 启动和从 Web 控制台启动自动同步时使用完全相同的通知、去重和隐私逻辑。

## 功能范围

- 当前分析结果为 P1/P2 的邮件产生 Windows 本地桌面提醒；
- 进入配置提醒窗口的可靠截止事项产生提醒；
- 工作流失败或带隔离失败完成时产生通用故障提醒；
- 每天在设定小时后的第一次运行生成一份摘要；
- 摘要包含近期 P1～P3 事项、截止日期、待人工复核数量、待确认动作和已批准待执行动作；
- SQLite 持久化每个通知事件的 SHA-256 指纹，服务重启后仍不会重复提醒同一邮件；
- 桌面提醒默认只显示优先级、数量和操作提示，不显示主题、发件人或完整正文。

第一版只支持 Windows 10/11 原生 Toast 和本地 Markdown 文件，不连接 Teams、Telegram 或其他
外部通知渠道，也不会上传邮件内容。

## 配置

复制最新的 `config/service.example.yaml` 后，可以在 `notifications` 段调整：

```yaml
notifications:
  enabled: true
  desktop_enabled: true
  daily_summary_enabled: true
  output_dir: data/private/summaries
  timezone: Asia/Shanghai
  daily_summary_hour: 18
  deadline_window_hours: 48
  summary_lookback_hours: 24
  retry_limit: 3
```

- `enabled`：关闭全部步骤六功能；
- `desktop_enabled`：只关闭桌面 Toast，保留每日摘要；
- `daily_summary_enabled`：只关闭摘要文件；
- `daily_summary_hour`：本地时区内第一次允许生成摘要的整点；
- `deadline_window_hours`：截止事项提醒和摘要的未来时间窗口；
- `summary_lookback_hours`：摘要中近期 P1～P3 邮件的回看窗口；
- `retry_limit`：桌面投递或摘要写入失败后的最大尝试次数。

旧的 `service.local.yaml` 没有 `notifications` 段时会使用以上安全默认值。若暂时不希望看到桌面提醒，
显式设置 `desktop_enabled: false`。

## 运行方式

通知已经接入现有服务命令，无需启动第二个进程：

```powershell
uv run inbox-agent service run-once --config config/service.local.yaml
uv run inbox-agent service start --config config/service.local.yaml
```

从 Web 控制台点击“启动自动同步”也会启用相同逻辑。每日摘要默认写入：

```text
data/private/summaries/YYYY-MM-DD.md
```

目录位于 Git 忽略的 `data/private/` 下。摘要使用临时文件加原子替换，避免进程中断留下半份文件。

## 去重与恢复

Alembic 迁移 `0004_notifications` 新增 `notification_deliveries` 表。该表只保存：

- 通知事件类型；
- SHA-256 去重键和关联对象哈希；
- pending、delivered 或 failed 状态；
- 尝试次数和时间；
- 不含邮件正文的安全错误类型。

P1/P2 去重键包含消息身份哈希、分析指纹和最终优先级；截止提醒还包含可靠截止时间；每日摘要按本地
日期去重；工作流故障按日期和错误类别去重。相同邮件在内容与分析不变时不会反复弹窗；内容变化并
产生新的分析指纹时可以重新提醒。

桌面投递失败不会改变工作流结果或触发 Outlook 重试。失败记录最多按 `retry_limit` 重试，避免每个
调度周期无限弹窗。后续步骤七会把通知成功率和最近错误加入统一统计与诊断命令。

## 隐私边界

- Windows Toast 不包含邮件主题、发件人、正文、Agent 摘要、Token 或 API Key；
- Markdown 摘要只包含主题、Agent 生成的有限摘要和截止时间，不包含完整正文；
- PowerShell 脚本为固定代码，通知文本通过子进程环境传递，不拼接进命令；
- PowerShell 标准输出和错误输出不会写入通知表或普通日志；
- 所有摘要、SQLite 和私有配置继续由 Git 忽略。

## 验收

自动化测试覆盖：

- P1/P2 和截止提醒首次投递；
- 第二次处理同一邮件时保持零重复投递；
- 工作流失败每日只提醒一次；
- 每日摘要每天只生成一次；
- 摘要包含优先事项、截止日期和人工处理计数；
- 桌面通知与摘要均不包含完整正文；
- 通知失败不会改变 `ServiceRunOutcome`；
- 旧数据库升级到 `0004_notifications`。

建议真实验收时先使用专用测试文件夹和一封合成 P1/P2 邮件，并把 `daily_summary_hour` 临时设置为
当前小时。确认 Toast 和摘要后恢复日常配置，不要把私有摘要提交到 Git。

## 真实邮箱与本地通知验收记录

2026-08-10，个人 Outlook 真实只读环境完成步骤六验收。执行前确认调度器未运行，并把 SQLite、
同步数据集、动作队列和审计日志备份到 Git 忽略的私有备份目录。验收结果如下：

- 旧数据库从 `0003_service` 自动升级至 `0004_notifications`，状态检查显示无需继续升级；
- 第一次 Delta 同步读取 38 封本地当前邮件，其中新增 1 封、分析 1 封并生成 1 个待确认动作；
- 新邮件的最终结果为 `P4 / general_notice`，因此不会错误触发 P1/P2 新邮件提醒；
- 当前高优先级邮件和可靠截止事项各产生 1 个汇总 Toast，Windows 投递进程成功返回；
- 通知账本分别记录 1 个 `priority_alert` 和 1 个 `deadline_alert`，状态均为 `delivered`；
- 受控生成当天 Markdown 摘要后，第二次调用返回零新摘要和 1 个重复事件；
- 摘要包含优先事项、即将到期、待处理和隐私说明四类结构；
- 将摘要与 38 封真实邮件的完整正文和长正文预览逐一匹配，命中数量均为 0；
- 第二次真实 Delta 同步新增、更新、分析、动作和审计计数均为 0，38 封全部跳过；
- 重复同步后通知账本仍只有优先级、截止和每日摘要各 1 条，没有产生重复投递；
- 两次工作流的 Graph 写请求均为 0，没有修改 Outlook 分类、位置、正文或附件。

真实邮件主题、发件人、Message ID、Token、私有摘要内容和本地通知指纹均未写入本文或 Git。
Windows 最终是否显示横幅仍受系统“勿扰/专注助手”设置影响；应用侧以 PowerShell 成功返回和
`delivered` 状态作为投递链路通过依据。
