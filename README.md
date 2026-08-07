# InboxPilot

InboxPilot 是一个面向 Microsoft 365 / Outlook 学生邮箱场景的可解释邮件优先级 Agent。它计划结合确定性规则、结构化 LLM 判断和 Microsoft Graph，将邮件划分为 P1～P5，并提取摘要、待办事项和截止时间。

## 项目状态

**阶段一：离线 MVP 已完成并通过验收。**

当前版本不连接 Outlook、不调用 LLM，也不会读取或修改真实邮箱。它使用匿名 JSON 邮件和 YAML 规则，在本地完成加载、清理、评分、分类、解释和回归评测，为后续接入模型与 Microsoft Graph 提供稳定内核。

阶段一成果：

- 20 封虚构匿名样例邮件，覆盖课程、考试、安全、行政、活动、推广和易误判场景；
- P1～P5 优先级、0～100 分、稳定类别、摘要、截止时间和人工复核标记；
- 每次分数变化均包含原因代码、说明、变化值和匹配内容；
- `demo`、`analyze`、`evaluate` 三个 CLI 命令，支持表格和 JSON 输出；
- 90 项自动化测试全部通过，总覆盖率 96.10%，最低门槛为 80%；
- pytest、Ruff、格式检查、mypy 和全新隔离环境安装验证全部通过。

详细证据见[阶段一验收报告](docs/stage1_acceptance.md)。

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

分析仓库中的 20 封匿名邮件：

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

### CLI 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令成功 |
| `1` | 数据集、人工标签或 YAML 配置无法加载或校验失败 |
| `2` | 分析完成，但至少一封邮件处理失败 |
| `3` | 评测完成，但预测与人工标签不一致 |

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
        ▼
   Rule Engine ── YAML 特征与可解释评分
        │
        ▼
    Pipeline ──── 分类、排序与失败隔离
        │
        ├────────► CLI Table / JSON
        │
        ▼
    Evaluator ◄── expected_results.json
```

## 项目结构

```text
inbox-pilot/
├── config/
│   └── rules.yaml                 # 可信地址、关键词、权重和阈值
├── data/
│   ├── samples/
│   │   └── sample_emails.json     # 20 封虚构匿名邮件
│   └── eval/
│       └── expected_results.json  # 独立人工期望结果
├── docs/
│   ├── rules_configuration.md     # YAML 规则修改指南
│   └── stage1_acceptance.md       # 阶段一验收报告
├── src/inbox_agent/
│   ├── cli.py                     # demo、analyze、evaluate
│   ├── evaluation.py              # 离线指标与不一致明细
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
- 阶段一不连接 Microsoft 365，不需要邮箱密码、客户端密钥或访问令牌；
- `.venv`、本地令牌、私有数据、日志和工具缓存不得提交到 Git；
- 后续 Microsoft Graph 集成默认采用委托权限、最小权限和只读模式；
- 群发只是一项信号，教务、考试和紧急安全通知即使群发也可以保持高优先级。

## 当前限制

- 尚未连接 Microsoft 365 / Outlook；
- 尚未使用 LLM 进行语义分类、摘要或任务提取；
- 当前规则和评测数据主要面向中文高校邮件场景；
- 离线回归指标不能代表真实邮箱中的泛化表现。

## 后续路线

1. 加入结构化 LLM 分类、摘要和截止日期提取；
2. 实现规则与 LLM 的置信度路由；
3. 通过 Microsoft Graph 实现委托登录和只读增量同步；
4. 加入人工确认，并在明确授权后写回 Outlook 分类；
5. 增加成本统计、Web 演示界面和 GitHub Actions 持续集成。

## License

项目许可证将在首次公开发布前确定。
