# InboxPilot

InboxPilot 是一个面向 Microsoft 365 / Outlook 邮箱的本地、可解释邮件分流 Agent。它结合
YAML 规则与可选的 OpenAI / DeepSeek 模型，为邮件生成 P1～P5 优先级、类别、摘要、待办事项、
截止时间和人工复核建议，并能在明确授权后把分类安全写回 Outlook。

项目可以通过 CLI 运行，也提供只监听本机回环地址的 FastAPI 接口。默认模式完全离线；邮箱、
LLM 和写回能力都必须由用户分别配置和授权。

## 功能概览

- 使用 Pydantic 严格加载和标准化 JSON 邮件，清理 HTML 正文；
- 使用 YAML 驱动的可解释规则生成 P1～P5 优先级及逐项评分原因；
- 按置信度和冲突信号选择性调用 OpenAI 或 DeepSeek，并进行保守融合；
- 通过 Microsoft Graph 委托登录，只读增量同步个人 Outlook 收件箱；
- 通过人工确认队列、dry-run、幂等键和写前冲突检查控制分类写回；
- 只允许修改一封邮件的 `categories`，不移动、不删除、不发送、不改写邮件内容；
- 对结果不确定的写入执行零 PATCH 对账，并支持保留用户分类的受控回滚；
- 使用 SQLite 持久化邮件、分析、动作、同步游标、工作流和服务状态；
- 通过统一增量工作流和单实例本地调度器持续同步与分析，跳过未变化邮件；
- 通过本地 FastAPI 查询邮件和解释结果，并复用既有审批、审计、写回、对账与回滚安全层；
- 提供 50 封匿名样例、离线评测、真实 LLM 验证和完整自动化测试。

## 项目状态

**阶段一至阶段三已经全部完成；阶段四步骤一至四已经完成，下一步为本地 Web 控制台。**

2026-08-09，阶段三在个人 Outlook 真实环境已完成以下验收：

- 单封邮件分类写入；
- 单封邮件真实受控回滚；
- 3 封邮件逐封正向写入；
- 3 封邮件逐封回滚清理；
- 每个动作均为一次 GET、最多一次 PATCH、零自动重试；
- 用户分类得到保留，邮件位置、主题、正文和附件状态不变。

同日，阶段四步骤一至三完成自动化与本地真实链路验收：

- SQLite 迁移、JSON 幂等导入和旧版本数据库升级通过；
- 统一工作流可持久化分析结果，第二次运行会跳过内容与配置均未变化的邮件；
- 本地调度器完成单次运行、单实例锁、状态保存、失败退避和优雅停止验收；
- 一封新到达的真实 Outlook 邮件被只读同步并生成 `P4 / general_notice` 待确认动作；
- 自动工作流保持 Graph 写请求为 0；人工批准后，受控执行器成功写入对应 Outlook 分类。
- 本地 FastAPI 文档、真实 SQLite 读取、筛选、审批、预览、单动作执行、对账和回滚接口通过验收；
- 写回与回滚接口要求显式 Action ID 确认，并继续复用既有写前检查、锁、审计和幂等控制。

当前自动化质量基线为 404 项测试通过，覆盖率 83.04%，Ruff 和 mypy 通过。真实邮件、令牌、
API Key、私有队列和审计日志均由 Git 忽略。

| 阶段 | 结果 | 详细文档 |
| --- | --- | --- |
| 阶段一 | JSON 加载、标准化、规则引擎、CLI 与离线评测完成 | [阶段一验收](docs/stage1_acceptance.md) |
| 阶段二 | 结构化 LLM、路由融合与 Outlook 只读同步完成 | [LLM Provider](docs/llm_provider.md)、[Outlook 同步](docs/microsoft_graph_sync.md) |
| 阶段 2.5 | 50 封样例与 DeepSeek 真实验证完成 | [DeepSeek 验证](docs/stage2_5_deepseek_validation.md) |
| 阶段三 | 人工确认、分类写回、审计、对账、回滚及真实小批量验收完成 | [单动作 CLI](docs/stage3_single_action_cli.md)、[受控回滚](docs/stage3_rollback.md) |
| 阶段四 | SQLite、统一增量工作流、单实例定时服务和本地 Web API 完成；Web 控制台待实现 | [SQLite 持久化](docs/stage4_sqlite_persistence.md)、[工作流编排](docs/stage4_workflow_orchestration.md)、[本地调度服务](docs/stage4_local_scheduler.md)、[FastAPI Web API](docs/stage4_fastapi_web_api.md) |

阶段四开发步骤：

- [x] 步骤一：SQLite 持久化与 Alembic 迁移；
- [x] 步骤二：同步、导入、增量分析和动作生成的统一工作流；
- [x] 步骤三：单实例本地调度器、状态探测与失败退避；
- [x] 步骤四：本地 FastAPI Web API；
- [ ] 步骤五：Jinja2 + HTMX 本地 Web 控制台（下一步）。

## 快速开始

### 环境要求

- Git
- [uv](https://docs.astral.sh/uv/)
- Python 3.12 或更高版本；也可以由 uv 自动管理

### 1. 本地安装

```powershell
git clone https://github.com/HirasawaNiji/inbox-pilot.git
Set-Location inbox-pilot
uv sync --locked
```

`uv sync --locked` 会根据 `uv.lock` 创建隔离环境，不会修改锁文件。

### 2. 运行离线 Demo

```powershell
uv run inbox-agent demo
```

这个命令分析仓库中的 50 封匿名虚构邮件，不需要邮箱、API Key 或网络权限。

常用输出选项：

```powershell
uv run inbox-agent demo --show-reasons
uv run inbox-agent demo --format json
```

### 3. 分析自己的 JSON 数据集

```powershell
uv run inbox-agent analyze path/to/messages.json `
  --config config/rules.yaml
```

输入必须符合 `MessageDataset` Schema。可从
[data/samples/sample_emails.json](data/samples/sample_emails.json) 查看完整示例。规则字段、关键词、权重
和阈值的修改方法见[规则配置指南](docs/rules_configuration.md)。

### 4. 初始化本地数据库（阶段四）

```powershell
uv run inbox-agent db init
uv run inbox-agent db import-json data/samples/sample_emails.json
uv run inbox-agent db status
```

数据库默认写入 Git 忽略的 `data/private/inbox_pilot.sqlite3`。重复初始化和重复导入均安全，
设计与验收方法见 [SQLite 持久化说明](docs/stage4_sqlite_persistence.md)。

统一执行导入、增量分析和人工确认动作生成：

```powershell
uv run inbox-agent workflow run `
  --dataset data/samples/sample_emails.json `
  --database data/private/stage4-test.sqlite3 `
  --queue data/private/stage4-test-actions.json `
  --audit-log data/private/stage4-test-audit.jsonl

uv run inbox-agent workflow status --database data/private/stage4-test.sqlite3
```

第二次运行会跳过内容与配置均未变化的邮件，且 Graph 写请求始终为 0。完整说明见
[工作流编排指南](docs/stage4_workflow_orchestration.md)。公开样例使用独立测试路径，避免把样例动作
混入真实 Outlook 人工确认队列。

需要定时运行时，复制安全模板并先执行单次验收：

```powershell
Copy-Item config/service.example.yaml config/service.local.yaml
uv run inbox-agent service run-once --config config/service.local.yaml
uv run inbox-agent service status --config config/service.local.yaml
```

长期运行使用 `service start`，按 `Ctrl+C` 优雅停止。配置和退避规则见
[本地调度服务指南](docs/stage4_local_scheduler.md)。

### 5. 启动本地 Web API（阶段四）

```powershell
uv run uvicorn inbox_agent.web.app:create_app `
  --factory `
  --host 127.0.0.1 `
  --port 8765
```

浏览器打开 <http://127.0.0.1:8765/docs> 查看和试用接口。当前 API 面向单用户本地控制台，
不要绑定到 `0.0.0.0`。危险操作仍要求动作先获批准，并在请求体中再次精确确认 Action ID。
接口、环境变量和安全边界见 [FastAPI Web API 指南](docs/stage4_fastapi_web_api.md)。

## 可选：接入 OpenAI 或 DeepSeek

复制本地配置模板：

```powershell
Copy-Item config/llm_provider.example.yaml config/llm_provider.local.yaml
```

在 `llm_provider.local.yaml` 中选择 Provider、模型、Base URL 和读取密钥的环境变量名。密钥只放入
当前终端环境，不要写进 YAML：

```powershell
$env:OPENAI_API_KEY = Read-Host -Prompt "请输入 OpenAI API Key" -MaskInput
```

DeepSeek 使用对应的环境变量：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host -Prompt "请输入 DeepSeek API Key" -MaskInput
```

运行分析：

```powershell
uv run inbox-agent analyze data/samples/sample_emails.json `
  --llm-config config/llm_provider.local.yaml
```

建议先执行一封低成本冒烟验证，再运行 50 封真实 Provider 评测：

```powershell
Copy-Item config/deepseek_validation.example.yaml config/deepseek_validation.local.yaml

uv run inbox-agent validate-llm `
  --llm-config config/deepseek_validation.local.yaml `
  --limit 1
```

完整配置、结构化输出、安全边界和故障排查见[真实 LLM Provider 接入指南](docs/llm_provider.md)；
50 封验证流程见[DeepSeek 真实验证指南](docs/stage2_5_deepseek_validation.md)。真实调用会消耗 Token
并产生费用。

## 可选：连接个人 Outlook

InboxPilot 使用 Microsoft Graph 委托权限，不需要邮箱密码或客户端密钥。

### 1. 配置只读同步

```powershell
Copy-Item config/graph.example.yaml config/graph.local.yaml
```

把个人 Entra 应用的 Client ID 填入 `graph.local.yaml`，然后执行：

```powershell
uv run inbox-agent outlook login --config config/graph.local.yaml
uv run inbox-agent outlook sync --config config/graph.local.yaml
uv run inbox-agent analyze data/private/outlook_inbox.json
```

本地配置、加密令牌缓存、Delta 状态和真实邮件都位于 Git 忽略路径。应用注册、个人 Outlook
兼容设置和同步故障排查见[Microsoft Graph 只读同步指南](docs/microsoft_graph_sync.md)。

### 2. 增量同步

首次同步后再次运行同一命令即可使用 Delta 状态拉取变化：

```powershell
uv run inbox-agent outlook sync --config config/graph.local.yaml
```

同步只读取收件箱字段，不下载附件二进制内容。

## 可选：把分类写回 Outlook

> **警告**：本节会真实修改 Outlook 邮件分类。请先在专用测试邮件上验收，并完整阅读
> [写权限基础](docs/stage3_write_permission_foundation.md)和
> [单动作执行指南](docs/stage3_single_action_cli.md)。

写权限使用独立配置、独立加密令牌缓存和 `Mail.ReadWrite`。它默认关闭，不能复用只读配置绕过
确认门。

### 1. 独立授权

```powershell
Copy-Item config/graph_write.example.yaml config/graph_write.local.yaml
uv run inbox-agent outlook write-login --config config/graph_write.local.yaml
```

检查权限后，才在私有 `graph_write.local.yaml` 中把 `write_enabled` 改为 `true`。该文件已被
`.gitignore` 排除。

### 2. 生成人工确认队列

```powershell
uv run inbox-agent actions build `
  --dataset data/private/outlook_inbox.json

uv run inbox-agent actions list
uv run inbox-agent actions show ACTION_ID --format json
```

### 3. 批准并生成零写入预览

```powershell
uv run inbox-agent actions approve ACTION_ID
uv run inbox-agent actions apply --dry-run --format json
```

从动作详情中复制正向幂等键，重复 Action ID 解锁单封执行：

```powershell
uv run inbox-agent actions execute ACTION_ID `
  --idempotency-key IDEMPOTENCY_KEY `
  --confirm-action ACTION_ID `
  --graph-config config/graph_write.local.yaml `
  --format json
```

该命令始终先重新读取实时分类和 `changeKey`。若批准后状态发生变化，它会返回冲突并保持零
PATCH。项目不提供批量执行入口。

### 4. 处理结果不确定状态

如果 PATCH 可能成功但响应未能验证，不要重新执行写入。使用一次 GET、零 PATCH 的对账命令：

```powershell
uv run inbox-agent actions reconcile ACTION_ID `
  --idempotency-key IDEMPOTENCY_KEY `
  --graph-config config/graph_write.local.yaml `
  --format json
```

### 5. 受控回滚

先生成回滚预览：

```powershell
uv run inbox-agent actions rollback ACTION_ID `
  --reason "分类结果不符合预期" `
  --dry-run `
  --format json
```

确认恢复目标后，使用预览生成的独立回滚幂等键：

```powershell
uv run inbox-agent actions rollback-execute ACTION_ID `
  --reason "分类结果不符合预期" `
  --rollback-idempotency-key ROLLBACK_KEY `
  --confirm-action ACTION_ID `
  --graph-config config/graph_write.local.yaml `
  --format json
```

回滚只恢复 `InboxPilot/` 命名空间，并保留实时存在的其他用户分类。结果不确定时使用
`actions rollback-reconcile`，不要盲目重试。完整状态机和命令见
[真实受控回滚指南](docs/stage3_rollback.md)。

## 常用命令

| 命令 | 用途 | 外部影响 |
| --- | --- | --- |
| `inbox-agent demo` | 分析 50 封公开样例 | 完全离线 |
| `inbox-agent analyze DATASET` | 分析指定数据集 | 默认离线；配置 LLM 后调用 Provider |
| `inbox-agent evaluate` | 与人工标签执行离线回归评测 | 完全离线 |
| `inbox-agent validate-llm` | 验证真实 LLM Provider | 消耗 API Token |
| `inbox-agent db init/status` | 初始化数据库或查看版本与计数 | 仅本地 SQLite |
| `inbox-agent db import-json DATASET` | 幂等导入并标准化现有 JSON 邮件 | 仅本地 SQLite |
| `inbox-agent workflow run` | 增量分析并生成待确认动作 | 默认离线；可选 Graph 只读和 LLM |
| `inbox-agent workflow status` | 查看最近持久化运行状态 | 仅本地 SQLite |
| `inbox-agent service run-once` | 使用服务配置执行一次锁定工作流 | 默认离线；可选 Graph 只读和 LLM |
| `inbox-agent service start/status` | 启动定时服务或查看实际锁与状态 | 自动同步/分析，不自动写回 |
| `uvicorn inbox_agent.web.app:create_app --factory` | 启动本地 Web API | 默认只读；写回接口必须显式确认 |
| `inbox-agent outlook login` | 获取只读委托授权 | 登录，不修改邮件 |
| `inbox-agent outlook sync` | 增量同步收件箱 | Graph 只读 |
| `inbox-agent actions build/list/show` | 管理本地动作队列 | 仅本地文件 |
| `inbox-agent actions apply --dry-run` | 预览分类差异 | Graph 写请求为 0 |
| `inbox-agent actions execute` | 执行一个已批准分类动作 | 最多一次 Graph PATCH |
| `inbox-agent actions reconcile` | 对账不确定的正向写入 | 一次 GET、零 PATCH |
| `inbox-agent actions rollback-execute` | 回滚一个成功动作 | 最多一次 Graph PATCH |
| `inbox-agent actions rollback-reconcile` | 对账不确定的回滚 | 一次 GET、零 PATCH |

查看全部参数：

```powershell
uv run inbox-agent --help
uv run inbox-agent actions --help
uv run inbox-agent outlook --help
uv run inbox-agent db --help
uv run inbox-agent workflow --help
uv run inbox-agent service --help
```

### CLI 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 配置、认证、确认门、Schema、队列或审计错误 |
| `2` | 分析部分失败，或 Graph 操作结果为冲突、失败、未知 |
| `3` | 离线评测或真实模型验证未达到门槛 |

## 配置文件

| 文件 | 用途 |
| --- | --- |
| `config/rules.yaml` | 关键词、可信地址、评分权重和 P1～P5 阈值 |
| `config/llm_routing.yaml` | 决定何时调用 LLM |
| `config/llm_fusion.yaml` | 规则与 LLM 的保守融合策略 |
| `config/llm_provider.example.yaml` | OpenAI / DeepSeek 本地配置模板 |
| `config/graph.example.yaml` | Outlook 只读同步模板 |
| `config/graph_write.example.yaml` | 默认关闭的独立写权限模板 |
| `config/service.example.yaml` | 使用隔离测试路径的本地调度模板 |

所有 `*.local.yaml`、API Key、令牌和私有邮件数据都不应提交到 Git。

## 安全与隐私边界

- 默认运行完全离线；
- LLM 密钥只从环境变量读取；
- Graph 使用委托权限和 OS 加密令牌缓存；
- 只读同步和写权限配置、作用域及令牌缓存互相隔离；
- 写回客户端只允许 Immutable Message ID、固定 Graph 消息端点、`PATCH` 和 `categories`；
- 每个动作必须人工批准，并使用幂等键和精确 Action ID 确认；
- Graph 写入前重新读取分类与 `changeKey`；
- 网络结果未知时禁止自动重试，只允许只读对账；
- 审计日志不保存邮件正文、主题、原始 Message ID、令牌或 API Key；
- SQLite 数据库默认位于 `data/private/`，不会提交到 Git；
- 移动、删除、发送邮件和修改主题、正文始终不在允许范围。

## 项目结构

```text
inbox-pilot/
├── config/              # 规则、LLM、Graph 公开模板
├── data/samples/        # 50 封匿名演示邮件
├── data/eval/           # 独立人工标签与离线 Fake 响应
├── docs/                # 配置、架构、安全与验收文档
├── src/inbox_agent/
│   ├── actions/         # 人工确认、审计、执行、对账与回滚
│   ├── graph/           # MSAL、Graph 同步和受限分类客户端
│   ├── llm/             # Provider、Prompt、路由、融合与验证
│   ├── storage/         # SQLite、SQLAlchemy Repository 与 Alembic 迁移
│   ├── workflow/        # 增量编排、运行状态与失败隔离
│   ├── service/         # 单实例定时运行、退避与状态探测
│   ├── web/             # 本地 FastAPI、查询服务与动作业务层适配
│   ├── loader.py        # JSON 加载与 Schema 校验
│   ├── normalizer.py    # HTML 与字段标准化
│   ├── rule_engine.py   # YAML 可解释规则引擎
│   └── pipeline.py      # 规则、LLM 与融合流水线
├── tests/               # 单元、集成、CLI 与安全回归测试
├── pyproject.toml
└── uv.lock
```

## 开发与质量检查

```powershell
uv run pytest
uv run pytest --cov=src/inbox_agent --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

提交前还建议运行：

```powershell
uv run inbox-agent evaluate
```

覆盖率最低门槛为 80%。

## 文档索引

### 使用与配置

- [YAML 规则配置](docs/rules_configuration.md)
- [OpenAI / DeepSeek Provider](docs/llm_provider.md)
- [分类 Prompt](docs/classification_prompt.md)
- [LLM 路由](docs/llm_routing.md)
- [规则与 LLM 融合](docs/llm_fusion.md)
- [Microsoft Graph 只读同步](docs/microsoft_graph_sync.md)
- [阶段四 SQLite 持久化](docs/stage4_sqlite_persistence.md)
- [阶段四统一工作流编排](docs/stage4_workflow_orchestration.md)
- [阶段四本地调度服务](docs/stage4_local_scheduler.md)
- [阶段四本地 FastAPI Web API](docs/stage4_fastapi_web_api.md)

### 分类写回与安全

- [人工确认队列](docs/stage3_review_queue.md)
- [分类 dry-run](docs/stage3_dry_run.md)
- [单动作执行与对账 CLI](docs/stage3_single_action_cli.md)
- [写权限与 Immutable ID](docs/stage3_write_permission_foundation.md)
- [Graph 分类写客户端](docs/stage3_graph_category_write_client.md)
- [受控执行器](docs/stage3_preflight_executor.md)
- [执行审计与结果对账](docs/stage3_execution_audit_and_reconciliation.md)
- [真实受控回滚](docs/stage3_rollback.md)

### 设计与验收记录

- [阶段一验收](docs/stage1_acceptance.md)
- [LLM 语义评测](docs/llm_evaluation.md)
- [阶段 2.5 DeepSeek 验证](docs/stage2_5_deepseek_validation.md)
- [阶段三动作模型](docs/stage3_action_models.md)
- [阶段三前半离线验收](docs/stage3_front_half_acceptance.md)
- [单封 Outlook 写入验收](docs/stage3_single_message_acceptance.md)
- [小批量 Outlook 验收](docs/stage3_small_batch_acceptance.md)

## 当前限制与后续方向

- 目前主要面向中文高校邮件和个人 Outlook 收件箱；
- 尚未验证组织租户策略、共享邮箱和其他邮件文件夹；
- 50 封公开样例和当前真实验收不能代表所有邮箱的泛化效果；
- 当前没有 Web UI、操作系统开机自启或无人确认的自动批量写回；
- 后续可增加 GitHub Actions、Web 演示和更大规模的匿名真实场景评测。

## License

项目许可证将在首次公开发布前确定。
