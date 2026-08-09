# 阶段四步骤三：本地定时调度服务

步骤三让阶段四工作流可以在个人电脑上长期、重复运行。它是一个单进程前台服务：保持终端
窗口运行即可定时同步和分析，按 `Ctrl+C` 会等待当前工作流结束并优雅停止。

服务仍然只执行以下自动操作：

```text
可选 Outlook 只读同步
→ 增量分析
→ 保存结果
→ 生成人工确认动作
```

服务不会自动批准动作，不会加载 Outlook 写权限配置，也不会执行 Graph PATCH。

## 准备本地配置

复制安全的公开模板：

```powershell
Copy-Item config/service.example.yaml config/service.local.yaml
```

`config/service.local.yaml` 已由 Git 忽略。公开模板默认：

- 使用 50 封匿名样例；
- 使用独立的 `service-test.*` 私有文件；
- 不连接 Outlook；
- 不调用 LLM；
- 每 15 分钟运行一次；
- 最大退避为 60 分钟。

不要在 YAML 中填写真实 API Key。若启用 LLM，只填写本地 Provider 配置路径，并继续通过环境
变量注入 Key。

## 手动运行一次

先使用与定时服务完全相同的配置、锁和状态机制运行一次：

```powershell
uv run inbox-agent service run-once `
  --config config/service.local.yaml `
  --format json
```

该命令运行结束后立即返回，不会等待下一轮。适合配置验收，也适合以后交给 Windows 任务计划
程序按固定时间调用。

## 启动定时服务

```powershell
uv run inbox-agent service start `
  --config config/service.local.yaml
```

服务默认立即运行一次，然后等待 `interval_minutes`。保持该 PowerShell 窗口打开；按 `Ctrl+C`
后，服务会把状态写为 `stopped` 并释放 OS 文件锁。

受控验收时可以只运行一次后自动退出：

```powershell
uv run inbox-agent service start `
  --config config/service.local.yaml `
  --max-runs 1 `
  --format json
```

`--max-runs` 限制的是实际工作流尝试次数，不会绕过锁、状态或安全边界。

## 查看状态

在另一个 PowerShell 窗口运行：

```powershell
uv run inbox-agent service status `
  --config config/service.local.yaml `
  --format json
```

状态同时检查：

- OS 文件锁是否确实被另一个进程持有；
- 数据库中的 `running`、`sleeping`、`backoff`、`idle` 或 `stopped` 状态；
- PID；
- 上次运行、成功和失败时间；
- 连续失败次数；
- 下次计划运行时间；
- 最近 Workflow Run ID；
- 最近错误摘要；
- 数据库 Revision 和是否需要升级。

`service status` 不读取邮件正文。数据库和锁文件均不存在时，也不会创建它们。

## 单实例和并发保护

服务启动时会对 `lock_path` 获取跨平台 OS 文件锁。Windows 使用 `msvcrt`，Unix 使用
`fcntl`。锁由进程持有，而不是仅依靠 PID 或锁文件是否存在。

如果另一个 `service start` 或 `service run-once` 已经持有锁，新进程会立即失败并显示：

```text
another InboxPilot service holds the lock
```

失败的第二实例不会覆盖第一个服务的数据库状态。进程异常退出后，操作系统会自动释放锁；残留
的空锁文件不代表服务仍在运行，`service status` 会实际探测锁。

## 失败退避

正常成功时，下一轮等待 `interval_minutes`。工作流抛出异常或以
`completed_with_failures` 结束时，服务使用有上限的指数退避：

```text
第 1 次连续失败：interval
第 2 次连续失败：interval × 2
第 3 次连续失败：interval × 4
……
最高不超过 max_backoff_minutes
```

例如 interval 为 15 分钟、最大退避为 60 分钟时，等待序列为 15、30、60、60……。下一次
完整成功后，连续失败计数清零并恢复正常 15 分钟间隔。

服务不会在同一工作流内部盲目重试 Graph 写入。步骤三调用的工作流本身没有 Graph 写能力；
退避只会启动下一轮同步和分析。

## 接入个人 Outlook

完成样例验收后，编辑 `config/service.local.yaml` 的 `workflow` 段：

```yaml
workflow:
  dataset_path: data/private/outlook_inbox.json
  database_path: data/private/inbox_pilot.sqlite3
  action_queue_path: data/private/action_queue.json
  audit_log_path: data/private/audit/actions.jsonl
  policy_path: config/rules.yaml
  llm_config_path: null
  llm_routing_path: config/llm_routing.yaml
  llm_fusion_path: config/llm_fusion.yaml
  sync_outlook: true
  graph_config_path: config/graph.local.yaml
```

然后先运行：

```powershell
uv run inbox-agent service run-once `
  --config config/service.local.yaml `
  --format json
```

它会使用之前保存的 `Mail.Read` 委托登录缓存。若缓存失效，应重新执行 `outlook login`，而不是
在服务配置中保存用户名或密码。

## 接入 DeepSeek 或 OpenAI

在 `workflow` 段设置：

```yaml
llm_config_path: config/llm_provider.local.yaml
```

启动服务前在同一 PowerShell 会话设置对应环境变量。服务配置、状态数据库和日志都不会保存
API Key。LLM 失败会触发退避，失败邮件会在下一轮重新尝试。

## 数据库升级

步骤三新增迁移 `0003_service`。已有 `0001_stage4` 或 `0002_workflow` 数据库无需删除：

```powershell
uv run inbox-agent db init
uv run inbox-agent db status --format json
```

升级只新增 `service_states` 表，不会删除邮件、分析结果、动作或工作流记录。

## 快速验收

```powershell
Copy-Item config/service.example.yaml config/service.local.yaml

uv run inbox-agent service run-once `
  --config config/service.local.yaml `
  --format json

uv run inbox-agent service run-once `
  --config config/service.local.yaml `
  --format json

uv run inbox-agent service start `
  --config config/service.local.yaml `
  --max-runs 1 `
  --format json

uv run inbox-agent service status `
  --config config/service.local.yaml `
  --format json
```

预期：第一轮分析 50 封；后续运行跳过 50 封；每次 Graph 写请求为 0；最终 `active` 为 false、
`persisted_status` 为 `stopped`、Revision 为 `0003_service`。
