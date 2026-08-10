# 阶段四步骤七：可观测性与故障恢复

步骤七让 InboxPilot 可以长期运行、定位问题并从本地数据库故障或误操作中恢复，同时保持此前的
隐私、人工确认、写前检查、锁、审计和幂等边界。

## 能力范围

- 使用 JSONL 结构化日志记录工作流、调度和 LLM 事件；
- 使用 Run ID 关联一次同步、分析和动作生成流程；
- 记录步骤耗时、LLM Token、可选费用估算和 Provider 成功率；
- 查询人工复核、待确认动作和通知积压；
- 使用邮件 Provider ID 的 SHA-256 标识定位处理链路；
- 使用 SQLite 在线备份 API 创建一致性备份；
- 在显式确认、服务锁检查、完整性检查和恢复前备份后执行恢复。

该步骤不会扩大 Outlook 写入范围。自动工作流仍保持
`graph_write_request_count = 0`，失败后的下一轮调度也不会直接重试 Graph 写回。

## 数据库迁移

迁移 `0005_observability` 新增 `observability_events` 表。每条事件可以包含：

- 时间、组件、操作和结果；
- Run ID；
- 邮件安全哈希，而不是原始 Message ID；
- 步骤或 Provider 耗时；
- Provider、模型、Token 和可选费用；
- 受限错误类型和不含正文的计数元数据。

升级现有数据库：

```powershell
uv run inbox-agent db init
uv run inbox-agent db status --format json
```

升级只新增事件表和索引，不删除现有邮件、分析、动作、同步游标、服务状态或通知记录。

## 结构化日志与隐私

服务配置示例：

```yaml
observability:
  enabled: true
  log_path: data/private/logs/inbox-pilot.jsonl
  llm_pricing: []
```

日志位于 Git 忽略的 `data/private/`。日志字段包括 Run ID、操作、结果、耗时和计数，但以下字段
会被集中脱敏：

- Authorization、Access Token、Refresh Token、API Key、密码和 Secret；
- 邮件主题、正文、正文预览和内容字段；
- 原始 Provider Message ID。

邮件追踪使用 `SHA-256(message_id)`。哈希只用于本机相关性查询，控制台不会回显用户输入的
原始 Message ID。遥测或 JSONL 写入失败属于辅助能力故障，不会改变同步、分类和调度结果。

## Token 与费用

Provider 返回 Token 用量时，InboxPilot 会保存输入、输出和缓存输入 Token。模型价格经常变化，
因此项目不硬编码价格。确认 Provider 当前价格后，可以在私有服务配置中添加：

```yaml
observability:
  enabled: true
  log_path: data/private/logs/inbox-pilot.jsonl
  llm_pricing:
    - provider: deepseek
      model_name: deepseek-v4-flash
      input_usd_per_million: "请填写当前价格"
      cached_input_usd_per_million: "请填写当前价格"
      output_usd_per_million: "请填写当前价格"
```

费用使用十进制定点运算并以百万分之一美元持久化。未配置价格、Provider 未返回用量或一次失败
调用的消费未知时，统计中的费用显示为 `null`，不会把未知费用错误地记为零。

## 诊断

```powershell
uv run inbox-agent doctor
uv run inbox-agent doctor --format json
```

`doctor` 是只读命令，检查：

- 数据库是否存在、Revision 是否为最新；
- SQLite `PRAGMA quick_check`；
- 调度锁是否活动和最近是否有受限错误；
- 日志与备份目录是否具备可写位置；
- 数据库是否位于推荐的 `data/private/`。

退出码：`0` 表示全部正常，`2` 表示存在非阻断警告，`1` 表示至少一个阻断错误。
该命令不会创建数据库、运行迁移、登录 Outlook 或调用 LLM。

## 统计和邮件追踪

```powershell
uv run inbox-agent stats
uv run inbox-agent stats --hours 168 --format json

uv run inbox-agent trace PROVIDER_MESSAGE_ID --format json
```

`stats` 提供工作流成功率和平均耗时、Provider 成功率、Token、可选费用、人工复核积压、动作积压、
通知积压以及最近错误类型。`trace` 在内存中散列输入，只返回邮件哈希以及导入、分析、LLM 和动作
生成事件，不返回主题、正文、发件人、原始 ID、Token 或 API Key。

## 一致性备份

```powershell
uv run inbox-agent backup
uv run inbox-agent backup --format json
```

备份默认写入 `data/private/backups/`，包括 SQLite 一致性备份，以及记录完整性检查、Alembic
Revision、文件大小和 SHA-256 的 JSON Manifest。备份使用 SQLite 在线备份 API，因此可以包含
WAL 状态的一致快照。备份仍包含私有邮件数据，不能上传到公开仓库或公共网盘。

## 受控恢复

首先完全停止自动同步。建议同时完全退出 Web 进程，避免恢复期间有页面继续读取数据库。然后：

```powershell
uv run inbox-agent restore `
  data/private/backups/inbox-pilot-YYYYMMDDTHHMMSSffffffZ.sqlite3 `
  --confirm `
  --format json
```

恢复按以下顺序执行：

1. 要求 `--confirm`；
2. 探测调度锁，服务活动时拒绝恢复；
3. 验证备份是完整 SQLite 数据库；
4. 验证备份 Revision 等于当前应用支持的最新 Revision；
5. 若目标数据库存在，自动创建 `pre-restore` 备份；
6. 恢复到临时文件并再次执行完整性检查；
7. 清理旧数据库的 WAL、SHM 或回滚日志旁路文件；
8. 原子替换目标数据库。

恢复不会自动启动服务。恢复后先运行：

```powershell
uv run inbox-agent doctor
uv run inbox-agent stats --format json
uv run inbox-agent service run-once --config config/service.local.yaml --format json
```

## 失败恢复与幂等边界

- 未变化邮件仍由内容哈希和分析配置指纹跳过；
- 通知仍由 SQLite 去重键跨重启去重；
- 动作写回仍要求已批准状态、幂等键、精确 Action ID、写前实时 GET 和进程锁；
- PATCH 结果不确定时禁止盲目重试，只允许零 PATCH 对账；
- 调度失败只触发下一轮同步/分析，不会自动执行或重试待确认写回。

因此，步骤七增加的日志、统计、备份和恢复入口不能绕过阶段三的写入安全层。

## 自动化验收

专项测试覆盖：

- Token、API Key、主题和正文不进入 JSONL；
- 邮件仅通过哈希追踪；
- Run、Provider、Token、耗时、费用与积压聚合；
- Windows 文件句柄下的一致性备份；
- 无确认、锁活动和损坏备份时拒绝恢复；
- 覆盖前自动备份和恢复后数据一致性；
- 既有写回幂等、未知结果对账和零盲目重试测试继续通过。
