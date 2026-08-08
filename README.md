# InboxPilot

InboxPilot 是一个面向 Microsoft 365 / Outlook 学生邮箱场景的可解释邮件优先级 Agent。它计划结合确定性规则、结构化 LLM 判断和 Microsoft Graph，将邮件划分为 P1～P5，并提取摘要、待办事项和截止时间。

## 项目状态

**阶段一、阶段二和阶段 2.5 已完成。下一步进入阶段三前半：人工确认、dry-run、审计、幂等与回滚。**

默认流程仍完全离线；用户显式创建本地 Graph 配置并完成授权后，可以只读同步个人 Outlook 收件箱。当前没有写回、移动、删除或发送邮件的能力。另在显式提供本地 LLM 配置和环境变量 API Key 后，可选择调用 OpenAI 或 DeepSeek 分析邮件。

阶段一成果：

- 20 封虚构匿名样例邮件，覆盖课程、考试、安全、行政、活动、推广和易误判场景；
- P1～P5 优先级、0～100 分、稳定类别、摘要、截止时间和人工复核标记；
- 每次分数变化均包含原因代码、说明、变化值和匹配内容；
- `demo`、`analyze`、`evaluate` 三个 CLI 命令，支持表格和 JSON 输出；
- 当前 233 项自动化测试全部通过，总覆盖率 92.17%，最低门槛为 80%；
- pytest、Ruff、格式检查、mypy 和全新隔离环境安装验证全部通过。

详细证据见[阶段一验收报告](docs/stage1_acceptance.md)。

### 阶段二进度

阶段二将保留阶段一的确定性规则作为安全基线，在此之上增加结构化 LLM 分析、置信度路由和 Microsoft Graph 只读接入。

步骤 1——结构化 LLM 分析基础与真实 Provider 已经完成：

- [x] 扩展任务、截止时间、LLM 输出、调用元数据和 Token 用量模型，并新增 14 项模型测试；
- [x] 定义可替换的 LLM Provider 接口、统一错误边界和离线 Fake Provider；
- [x] 设计 `triage-v1` 版本化分类 Prompt，并绑定严格 Pydantic 响应 Schema；
- [x] 将可选 LLM Provider 接入 Pipeline，并独立保留原始结构化结果用于审计；
- [x] 新增 8 封独立语义边界邮件、人工标签和严格 Fake 响应，评测摘要、任务与截止时间提取；
- [x] 接入可由 YAML 切换的 OpenAI / DeepSeek 真实 Provider，密钥只从环境变量读取；
- [x] OpenAI 使用原生 Structured Outputs，DeepSeek 使用 JSON 模式并在本地严格复验；
- [x] `analyze` 支持选择性真实模型调用，记录耗时、Token、请求 ID、路由与融合结果。

后续步骤：

- [x] 根据规则置信度和冲突信号决定是否调用 LLM，并记录可解释路由原因；
- [x] 保守融合规则与 LLM 判断，阻止静默降级，并对优先级、截止时间和置信度冲突强制复核；
- [x] 实现 Microsoft Graph 委托登录、OS 加密令牌缓存和只读 Delta 增量同步；
- [x] 将 Graph 邮件严格转换为现有数据模型，不下载附件，并把真实数据隔离在 `data/private/`；
- [x] 使用个人 Outlook Client ID 完成设备码登录、首次同步、增量同步和 Pipeline 分析验收。

Microsoft Graph 真实环境验收已于 2026-08-08 完成。其他开发者可按照[个人 Outlook 只读同步指南](docs/microsoft_graph_sync.md)连接自己的邮箱。

### 阶段 2.5 进度

- [x] 将主 demo 从 20 封扩充到 50 封公开虚构邮件；
- [x] 将独立人工标签扩充到 50 条，并在 `2.1` 中加入 LLM 期望覆盖与显式优先级容差；
- [x] 新增全量真实 Provider 验证命令，统计准确率、失败、Token 和耗时；
- [x] DeepSeek V4 分类请求使用 JSON Output、本地严格 Schema，并关闭 thinking 模式；
- [x] 使用本地 API Key 完成首次 50 封 DeepSeek 真实验证并记录 `triage-v3` 基线；
- [x] 使用显式优先级容差重新验证 `triage-v4`；本地验收在允许范围内达到 80%，阶段 2.5 基本通过。

阶段 2.5 的配置、安全边界、验收门槛、运行命令和真实验证记录见[阶段 2.5 DeepSeek 真实验证指南](docs/stage2_5_deepseek_validation.md)。OpenAI / DeepSeek 的配置、密钥保护与故障排查见[真实 LLM Provider 接入指南](docs/llm_provider.md)。Outlook 分类写回已移动到阶段三后半部分；只有人工确认、dry-run、审计、幂等和回滚机制先通过验收后，才会考虑提升邮箱权限。

## 快速开始

### 环境要求

- Git
- [uv](https://docs.astral.sh/uv/)
- Python 3.12 或更高版本（可由 uv 管理）

### 安装与运行

```powershell
git clone https://github.com/HirasawaNiji/inbox-pilot.git
Set-Location inbox-pilot
uv sync --locked
uv run inbox-agent demo
```

`uv sync --locked` 会按照 `uv.lock` 创建隔离环境并安装运行和开发依赖，不会重新解析或修改锁文件。

## CLI 使用

查看命令帮助：

```powershell
uv run inbox-agent --help
```

### `demo`：运行内置样例

分析仓库中的 50 封匿名邮件：

```powershell
uv run inbox-agent demo
```

显示每项评分原因：

```powershell
uv run inbox-agent demo --show-reasons
```

输出机器可读 JSON：

```powershell
uv run inbox-agent demo --format json
```

### `analyze`：分析指定数据集

```powershell
uv run inbox-agent analyze data/samples/sample_emails.json
```

指定 YAML 规则并输出 JSON：

```powershell
uv run inbox-agent analyze data/samples/sample_emails.json `
  --config config/rules.yaml `
  --format json
```

选择 OpenAI 或 DeepSeek 进行真实结构化分析：

```powershell
Copy-Item config/llm_provider.example.yaml config/llm_provider.local.yaml
$env:OPENAI_API_KEY = Read-Host -Prompt "请输入 OpenAI API Key" -MaskInput
uv run inbox-agent analyze data/samples/sample_emails.json `
  --llm-config config/llm_provider.local.yaml
```

DeepSeek 只需修改本地 YAML 中的 `provider`、`model`、`base_url` 和 `api_key_env`。完整接入步骤、两类运行模式、验收指标、密钥保护和故障排查见[OpenAI / DeepSeek Provider 配置指南](docs/llm_provider.md)。

先对 1 封公开样例执行低成本冒烟测试，再运行全部 50 封：

```powershell
Copy-Item config/deepseek_validation.example.yaml config/deepseek_validation.local.yaml
$env:DEEPSEEK_API_KEY = Read-Host -Prompt "请输入 DeepSeek API Key" -MaskInput
uv run inbox-agent validate-llm `
  --llm-config config/deepseek_validation.local.yaml `
  --limit 1

uv run inbox-agent validate-llm `
  --llm-config config/deepseek_validation.local.yaml
```

DeepSeek 会收到完整响应 Schema，返回结果仍由 Pydantic 在本地严格复验。命令会产生 API 调用和费用；任一 Provider、Schema 或融合错误会停止后续 LLM 请求并显示具体错误。首次运行前请阅读[阶段 2.5 DeepSeek 真实验证指南](docs/stage2_5_deepseek_validation.md)。

### `evaluate`：执行离线回归评测

将 Pipeline 预测与独立人工标签比较：

```powershell
uv run inbox-agent evaluate
```

输出评测 JSON：

```powershell
uv run inbox-agent evaluate --format json
```

评测报告包含优先级准确率、类别准确率、人工复核一致率、P1 精确率、P1 召回率和不一致明细。样例数据集上的 100% 结果用于防止规则回归，不代表真实邮箱中的泛化准确率。

### `outlook`：登录与只读同步

先复制 Graph 配置模板，并把个人 Entra 应用的 Client ID 写入本地配置：

```powershell
Copy-Item config/graph.example.yaml config/graph.local.yaml
uv run inbox-agent outlook login --config config/graph.local.yaml
uv run inbox-agent outlook sync --config config/graph.local.yaml
uv run inbox-agent analyze data/private/outlook_inbox.json
```

`graph.local.yaml`、令牌缓存、Delta 状态和真实邮件均由 `.gitignore` 排除。完整的应用注册、字段说明、安全检查和验收步骤见[个人 Outlook 只读同步指南](docs/microsoft_graph_sync.md)。

### CLI 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令成功 |
| `1` | 数据集、人工标签、YAML 配置或 API Key 环境变量无法加载或校验失败 |
| `2` | 分析或真实模型验证完成，但至少一封规则或 LLM 分析失败 |
| `3` | 离线评测不一致，或真实模型验证指标未达到门槛 |

## 阶段一实现范围

### 数据模型与 JSON 加载

- 使用 Pydantic 定义原始邮件、标准化邮件、评分原因、分类结果和数据集；
- 校验必填字段、邮箱地址、时间、附件和枚举值；
- 拒绝非法 JSON、重复来源 ID 和不符合 Schema 的输入。

### HTML 清理与标准化

- 将 HTML 正文转换为纯文本；
- 移除脚本、样式、常见签名和多余空白；
- 统一邮箱地址大小写；
- 提取发件人域名、收件人数、退订特征和可供规则使用的标准字段。

### YAML 可解释规则引擎

- 从 `config/rules.yaml` 加载可信地址、关键词、权重和优先级阈值；
- 综合可信发件人、重要性、紧急/安全/行动关键词、截止日期、群发、附件、退订和发件人差异等信号；
- 将最终分数限制在 0～100，并映射到 P1～P5；
- 对空标题、临界分数和信号冲突等情况标记人工复核；
- 为每个加减分项输出可追踪的 `reason_code`。

修改规则前请阅读[YAML 规则配置指南](docs/rules_configuration.md)。

### Pipeline 与离线评测

- Pipeline 统一组织加载、标准化、特征提取、规则评分、分类和排序；
- 单封邮件异常会被隔离并记录，不会直接中止整批分析；
- 人工期望结果与预测逻辑分离，便于发现规则回归；
- 评测覆盖优先级、类别、人工复核标记以及 P1 精确率和召回率。

### 阶段二数据契约

- `ExtractedDeadline`：保存时区明确的截止时间、显式/推断类型、置信度和原文证据；
- `ActionItem`：保存结构化任务、置信度、证据和可选截止时间；
- `LLMMessageAnalysis`：约束模型必须返回的优先级、类别、摘要、任务、截止时间和简短判断依据；
- `LLMAnalysisResult`：记录 Provider、模型名称、Prompt 版本、耗时、Token 用量和请求 ID；
- `TriageResult`：继续作为 CLI、存储和未来 UI 使用的稳定公共结果，并支持结构化任务。

这些模型同时约束 Fake Provider、OpenAI Structured Outputs 和 DeepSeek JSON 响应。

### LLM Provider 抽象

- `LLMProvider`：Pipeline 面向的可替换接口，不依赖具体模型 SDK；
- `FakeLLMProvider`：按照邮件 `source_id` 返回预设分析，可在完全离线环境中开发和测试；
- `LLMProviderError`：统一缺失响应、服务不可用和输出契约失败等错误边界；
- Fake Provider 支持固定时钟、Token 用量、耗时、请求编号、调用记录和失败模拟。

默认命令不配置真实 Provider，也不需要 API Key；显式传入 `--llm-config` 后才会调用外部服务。配置方法见[Provider 配置指南](docs/llm_provider.md)。

### 分类 Prompt

- `triage-v4` 固定 P1～P5 标准、稳定类别、截止时间和人工复核语义，细化严重后果、类别边界和复核触发，并向 JSON 模式 Provider 提供完整响应 Schema；
- 系统指令与邮件 JSON 分离，邮件内容始终按不可信数据处理；
- 默认最多发送 12,000 个正文字符，并明确记录是否截断；
- `MessageCategory` 将分类限制在已知枚举内；
- `LLMMessageAnalysis` 生成严格 JSON Schema，禁止额外字段；
- OpenAI 将 Schema 直接用于 Structured Outputs；DeepSeek 返回 JSON 后使用同一 Schema 本地校验。

详细设计见[分类 Prompt 设计文档](docs/classification_prompt.md)。

### Pipeline 智能分析

- `OfflinePipeline` 可以接收可选的 `LLMProvider`；
- 默认选择性路由只调用低置信度、要求复核或存在冲突信号的邮件；
- 未调用 LLM 的结果保持 `decision_source=rule`；成功应用融合后使用 `decision_source=hybrid`；
- LLM 原始结构化结果继续单独写入 `llm_analyses`，方便审计和独立评测；
- LLM 缺失响应、Provider 故障和消息 ID 不匹配会写入 `llm_failures`；
- 旁路失败不增加规则 `failure_count`，也不会阻止剩余邮件处理；
- CLI 默认不配置 Provider；只有 `analyze --llm-config ...` 会读取环境变量并调用 LLM。

### OpenAI / DeepSeek 真实 Provider

- 使用同一 OpenAI Python SDK 和 `LLMProvider` 接口，通过 YAML 切换服务；
- API Key 只从 `api_key_env` 指定的环境变量读取，不进入配置文件；
- OpenAI 使用严格 JSON Schema，DeepSeek 使用 JSON Object 模式并由 Pydantic 复验；
- 拒答、空响应、截断、非法 Schema 和 HTTP 故障都有明确错误类型；
- 单封 LLM 故障不覆盖规则结果，但 CLI 会以退出码 `2` 提醒本次运行并非全部成功；
- 网络测试全部使用模拟传输，不消耗 API 额度。

详细字段、切换方法和首次安全测试步骤见[OpenAI / DeepSeek Provider 配置指南](docs/llm_provider.md)。

### Microsoft Graph 只读同步

- 使用 MSAL 设备代码流进行委托登录，不保存邮箱密码，也不使用 Client Secret；
- 权限配置被限制为 `Mail.Read`，HTTP 边界只允许访问 Graph 邮件 Delta 端点并发送 `GET` 请求；
- 令牌缓存使用操作系统安全存储加密，且与同步状态、真实邮件一起保存在 `data/private/`；
- 首次读取最近指定天数的 Inbox 邮件，随后通过 Delta Link 增量处理新增、更新和移除；
- 请求 Immutable ID 以减少邮件移动导致的 ID 变化；
- Graph 响应经严格 Pydantic 校验后映射为现有 `EmailMessage`，只记录附件存在性，不下载附件；
- 只有完整分页且所有邮件均转换成功才推进 Delta 状态，避免部分失败造成永久漏信。

实现同时通过模拟 Graph 响应的离线测试，以及个人 Outlook 的设备码登录、首次分页同步、Delta 增量同步和 Pipeline 分析验收。连接自己的邮箱请阅读[Microsoft Graph 个人 Outlook 只读同步指南](docs/microsoft_graph_sync.md)。

### 可解释 LLM 调用路由

- `config/llm_routing.yaml` 配置选择性/全量模式、置信度阈值和六项路由开关；
- `LLMRoutingDecision` 记录是否调用、规则置信度、策略版本及全部原因；
- 当前覆盖模糊日期、多个截止时间、行动与无需行动冲突、高重要性与低价值内容冲突；
- `llm_routed_count` 和 `llm_skipped_count` 可用于后续成本与隐私暴露统计；
- 全量模式专门用于离线语义评测，避免选择性路由影响指标完整性。

配置字段、原因代码和调用示例见[LLM 调用路由配置指南](docs/llm_routing.md)。

### 规则与 LLM 受控融合

- `config/llm_fusion.yaml` 配置最低 LLM 置信度和优先级、截止时间安全边界；
- 规则分数、评分原因和策略版本始终保留；
- 高置信度 LLM 可补充类别、摘要、行动项和截止时间；
- 默认允许提升到更紧急优先级，但禁止 LLM 静默降低规则优先级；
- 优先级冲突、截止时间冲突、低置信度或模型主动复核都会强制人工确认；
- `LLMFusionDecision` 保存双方优先级、置信度、最终结果及全部融合原因。

详细算法和配置说明见[规则与 LLM 受控融合指南](docs/llm_fusion.md)。

### 阶段二 LLM 语义评测

- 保留 8 封独立语义边界邮件，与阶段 2.5 的 50 封主 demo 分开评测；
- 覆盖相对日期、多任务、无任务、Prompt Injection、模糊/冲突日期、转发旧截止时间和英文通知；
- 人工标签与 Fake Provider 响应分开保存，避免把预设响应直接当成评测标准；
- 分别计算优先级、类别、摘要事实、行动项、截止时间和人工复核准确率；
- 缺失响应和 Provider 故障会形成明确的不一致记录，但不影响规则结果。

数据含义、指标算法和修改流程见[阶段二 LLM 旁路评测指南](docs/llm_evaluation.md)。

## 数据流

```text
sample_emails.json
        │
        ▼
   JSON Loader ── Pydantic 校验
        │
        ▼
   Normalizer ─── HTML / 字段标准化
        │
        └────────► Rule Engine ───► Rule TriageResult
                                  │
                                  ▼
                             LLM Router
                            ┌─────┴─────┐
                            ▼           ▼
                          跳过      LLM Provider
                            │           │
                            │           ▼
                            │     LLMAnalysisResult
                            │           │
                            └─────┬─────┘
                                  ▼
                            Fusion Engine
                                  │
                                  ▼
                         最终 TriageResult / AnalysisReport
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
       CLI Table / JSON             Evaluator / 人工标签
```

## 项目结构

```text
inbox-pilot/
├── config/
│   ├── rules.yaml                        # 可信地址、关键词、权重和阈值
│   ├── llm_routing.yaml                  # LLM 置信度与冲突路由
│   ├── llm_fusion.yaml                   # 规则与 LLM 安全融合
│   ├── llm_provider.example.yaml         # OpenAI / DeepSeek 公开配置模板
│   ├── deepseek_validation.example.yaml  # 50 封真实模型验证模板
│   └── graph.example.yaml                # 个人 Outlook 只读同步配置模板
├── data/
│   ├── samples/
│   │   ├── sample_emails.json            # 阶段 2.5 的 50 封主 demo
│   │   └── llm_evaluation_emails.json    # 阶段二 8 封语义边界邮件
│   └── eval/
│       ├── expected_results.json         # 阶段 2.5 的 50 条人工标签
│       ├── expected_llm_results.json     # 阶段二人工语义标签
│       └── fake_llm_responses.json       # 严格离线结构化预测
├── docs/
│   ├── classification_prompt.md          # triage-v4 Prompt 设计
│   ├── llm_evaluation.md                 # 阶段二语义评测指南
│   ├── llm_fusion.md                     # 规则与 LLM 受控融合指南
│   ├── llm_provider.md                   # 真实 Provider 配置与安全指南
│   ├── llm_routing.md                    # LLM 调用路由配置指南
│   ├── microsoft_graph_sync.md            # 个人 Outlook 应用注册与同步指南
│   ├── rules_configuration.md            # YAML 规则修改指南
│   ├── stage2_5_deepseek_validation.md    # 50 封 DeepSeek 真实验证指南
│   └── stage1_acceptance.md              # 阶段一验收报告
├── src/inbox_agent/
│   ├── cli.py                     # demo、analyze、evaluate、validate-llm、outlook
│   ├── evaluation.py              # 离线指标与不一致明细
│   ├── graph/                      # MSAL 登录、Graph GET、映射与 Delta 同步
│   ├── llm/                        # Provider、Prompt、路由、融合与真实验证指标
│   ├── loader.py                  # JSON 读取与校验
│   ├── models.py                  # Pydantic 数据模型
│   ├── normalizer.py              # HTML 与字段标准化
│   ├── pipeline.py                # 离线分析流水线
│   └── rule_engine.py             # YAML 可解释规则引擎
├── tests/                         # 单元、集成、评测和 CLI 测试
├── pyproject.toml                 # 项目、依赖和工具配置
├── uv.lock                        # 可复现依赖锁文件
└── README.md
```

## 开发与质量检查

运行全部测试：

```powershell
uv run pytest
```

运行测试并检查 80% 覆盖率门槛：

```powershell
uv run pytest --cov=src/inbox_agent --cov-report=term-missing
```

运行代码规范、格式和静态类型检查：

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

修改规则或代码后的推荐回归流程：

```powershell
uv run inbox-agent evaluate
uv run pytest --cov=src/inbox_agent --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

## 阶段一验收结果

- [x] 全新隔离环境执行 `uv sync --locked` 后可以运行项目；
- [x] `inbox-agent demo` 无需邮箱或 API Key 即可运行；
- [x] 包含 20 封虚构匿名样例邮件；
- [x] 输入数据经过 Pydantic 校验；
- [x] HTML 正文可以转换为纯文本；
- [x] 规则配置与 Python 代码分离；
- [x] 每个评分变化都有可解释原因；
- [x] 输出支持 P1～P5 和 0～100 分；
- [x] 重要群发邮件不会被简单判定为低优先级；
- [x] 支持终端表格和 JSON 输出；
- [x] pytest、Ruff、格式检查和 mypy 全部通过；
- [x] 测试覆盖率达到 96.10%，超过 80% 门槛；
- [x] 仓库中不存在真实邮件和凭据。

阶段一已于 2026-08-07 完成全部验收。

## 隐私与安全边界

- 演示邮件必须完全虚构，并使用 `example.edu`、`example.com` 等保留域名；
- 不得提交真实姓名、学号、邮箱地址、邮件正文或学校内部链接；
- 阶段一不连接 Microsoft 365；Graph 接入也不需要邮箱密码或客户端密钥，只使用委托访问令牌；
- `.venv`、本地令牌、私有数据、日志和工具缓存不得提交到 Git；
- Microsoft Graph 集成采用委托权限、`Mail.Read` 最小权限和只读模式；
- Graph 本地配置、加密令牌缓存、Delta 状态和真实邮件默认保存在 Git 忽略路径；
- 群发只是一项信号，教务、考试和紧急安全通知即使群发也可以保持高优先级。

## 当前限制

- Microsoft Graph 已通过个人 Outlook 验收，但尚未验证组织租户策略、共享邮箱和其他邮件文件夹；
- 已接入 OpenAI / DeepSeek API，但尚未使用真实邮箱数据进行泛化评测；
- 50 封 DeepSeek `triage-v3` 精确基线已记录；`triage-v4` 本地复测按显式容差口径达到 80% 并基本通过，但仍需更多真实场景检验泛化能力；
- 当前规则和评测数据主要面向中文高校邮件场景；
- 离线回归指标不能代表真实邮箱中的泛化表现。

## 后续路线

1. [x] 接入真实结构化 LLM Provider，并保留独立语义数据集评测；
2. [x] 实现 Microsoft Graph 委托登录和只读增量同步；
3. [x] 使用个人 Outlook 账号完成 Graph 真实环境验收；
4. [x] 将主 demo 扩展到 50 封，并实现 DeepSeek 全量验证与 Token 统计；
5. [x] 完成首次 DeepSeek 真实运行并记录 `triage-v3` 基线；
6. [x] 复测 `triage-v4`，按显式容差口径完成阶段 2.5 基本验收；
7. [ ] 阶段三前半：实现人工确认、dry-run、审计、幂等和回滚机制；
8. [ ] 阶段三后半：在明确授权后申请写权限并写回 Outlook 分类；
9. [ ] 增加 Web 演示界面和 GitHub Actions 持续集成。

## License

项目许可证将在首次公开发布前确定。
