# InboxPilot

InboxPilot 是一个面向 Microsoft 365 / Outlook 学生邮箱的可解释邮件优先级 Agent。项目计划结合规则引擎、结构化 LLM 判断和 Microsoft Graph，将邮件划分为 P1～P5 优先级，并提取摘要、待办事项和截止时间。

当前仓库处于**阶段一：离线 MVP**。现阶段首先构建不依赖真实邮箱和外部模型的邮件分析内核，确保任何人都可以使用匿名样例数据运行、测试和理解项目。

## 当前进度

已完成：

- [x] 创建 Git 仓库，默认分支为 `main`
- [x] 创建基础项目目录
- [x] 安装并配置 uv
- [x] 创建 Python 3.12 虚拟环境 `.venv`
- [x] 使用 `pyproject.toml` 和 `uv.lock` 管理依赖
- [x] 安装阶段一运行依赖和开发依赖

阶段一功能：

- [x] 定义原始邮件、标准化邮件和分类结果模型
- [x] 实现 JSON 邮件加载器
- [x] 实现 HTML 清理与邮件标准化
- [x] 实现 YAML 驱动的可解释规则引擎
- [x] 准备匿名样例邮件
- [x] 实现离线分析流水线
- [x] 完善 CLI 命令和输出
- [x] 实现离线评测命令
- [x] 编写单元测试
- [x] 配置 Ruff、mypy 和 pytest
- [x] 完成本轮阶段一验收（全新环境安装验证按计划暂缓）

> 阶段一离线 MVP 已通过当前范围内的最终验收；全新环境执行 `uv sync` 的验证暂缓，详见[阶段一验收报告](docs/stage1_acceptance.md)。

## 阶段一目标

阶段一不连接 Outlook、不调用 LLM，也不修改真实邮件。目标是完成一个完全离线的邮件分析 MVP：

1. 从 JSON 文件加载匿名邮件。
2. 校验并标准化邮件字段。
3. 清理 HTML、签名和多余空白。
4. 根据可信发件人、紧急关键词、收件人数、附件和退订信息等信号评分。
5. 输出 P1～P5 优先级、0～100 分和可解释原因。
6. 使用 pytest 对规则和边界情况进行验证。

计划中的使用方式：

```powershell
uv run inbox-agent demo
uv run inbox-agent analyze data/samples/sample_emails.json
uv run pytest
```

## CLI 使用

运行内置的 20 封匿名样例邮件：

```powershell
uv run inbox-agent demo
```

显示每项可解释评分原因：

```powershell
uv run inbox-agent demo --show-reasons
```

输出机器可读的 JSON：

```powershell
uv run inbox-agent demo --format json
```

分析指定的数据集：

```powershell
uv run inbox-agent analyze data/samples/sample_emails.json
```

指定自定义 YAML 规则并输出 JSON：

```powershell
uv run inbox-agent analyze data/samples/sample_emails.json `
  --config config/rules.yaml `
  --format json
```

表格输出包含优先级、分数、类别、复核状态、截止时间和摘要。JSON 输出包含完整的评分原因，可用于后续评测、Web 页面或 Microsoft Graph 集成。

将 Pipeline 预测与独立人工标签进行比较：

```powershell
uv run inbox-agent evaluate
```

输出机器可读的评测 JSON：

```powershell
uv run inbox-agent evaluate --format json
```

评测报告包含优先级准确率、类别准确率、人工复核一致率、P1 精确率、P1 召回率以及所有预测不一致明细。该结果只代表匿名回归数据集，不代表真实邮箱中的泛化准确率。

## 环境要求

| 工具 | 用途 |
| --- | --- |
| Windows PowerShell | 当前开发终端 |
| Git | 版本控制 |
| uv | Python、虚拟环境、依赖和锁文件管理 |
| Python 3.12 | 项目运行环境 |

当前虚拟环境路径：

```text
E:\inbox-pilot\.venv
```

推荐通过 `uv run` 执行命令，不需要手动激活虚拟环境：

```powershell
Set-Location E:\inbox-pilot
uv run python --version
```

## 依赖说明

### 运行依赖

| 依赖 | 用途 |
| --- | --- |
| `pydantic` | 定义并校验邮件、规则配置和分类结果的数据结构 |
| `typer` | 构建 `inbox-agent` 命令行界面 |
| `rich` | 在终端中展示彩色表格、错误和评分原因 |
| `pyyaml` | 从 `config/rules.yaml` 加载关键词、白名单和评分权重 |
| `beautifulsoup4` | 将 HTML 邮件正文清理为适合分析的纯文本 |
| `python-dateutil` | 处理 ISO 时间、时区和基础日期解析 |

### 开发依赖

| 依赖 | 用途 |
| --- | --- |
| `pytest` | 编写和运行单元测试 |
| `pytest-cov` | 统计测试覆盖率 |
| `ruff` | 代码检查和格式规范 |
| `mypy` | 静态类型检查 |

依赖由以下文件管理：

- `pyproject.toml`：声明直接依赖和项目元数据。
- `uv.lock`：锁定完整依赖树，保证不同环境安装结果一致。

同步环境：

```powershell
uv sync
```

新增运行依赖：

```powershell
uv add <package-name>
```

新增开发依赖：

```powershell
uv add --dev <package-name>
```

## 文件架构

以下是阶段一计划完成的结构：

```text
inbox-pilot/
├── src/
│   └── inbox_agent/
│       ├── __init__.py       # Python 包入口
│       ├── cli.py            # demo、analyze、evaluate 等 CLI 命令
│       ├── evaluation.py     # 加载人工标签并计算离线评测指标
│       ├── models.py         # 邮件、评分原因和分类结果模型
│       ├── loader.py         # 从 JSON 加载并校验邮件
│       ├── normalizer.py     # HTML 清理、空白处理和字段标准化
│       ├── rule_engine.py    # 可解释的规则评分引擎
│       └── pipeline.py       # 组织加载、标准化、评分和输出
├── data/
│   ├── samples/
│   │   └── sample_emails.json # 虚构且匿名的演示邮件
│   └── eval/
│       └── expected_results.json # 与预测逻辑分离的人工期望结果
├── config/
│   └── rules.yaml            # 白名单、关键词、权重和阈值
├── tests/
│   ├── test_loader.py
│   ├── test_normalizer.py
│   ├── test_rule_engine.py
│   ├── test_pipeline.py
│   ├── test_evaluation.py
│   └── test_cli.py
├── docs/                     # 架构、安全和评测文档
├── pyproject.toml            # 项目配置与直接依赖
├── uv.lock                   # 可复现依赖锁文件
├── README.md
└── .gitignore
```

### 模块职责

`models.py`

- `EmailMessage`：对应 JSON 或未来 Graph API 返回的单封原始邮件。
- `MessageDataset`：封装带版本号的邮件集合，并阻止重复的来源 ID。
- `NormalizedMessage`：保存清洗后、适合规则分析的数据。
- `TriageResult`：保存 P1～P5、分数、原因及是否需要复核。

`loader.py`

- 读取 UTF-8 JSON 文件。
- 使用 Pydantic 校验字段。
- 检查缺失字段、非法时间和重复邮件 ID。

`normalizer.py`

- 将 HTML 转换为纯文本。
- 移除脚本、样式和多余空白。
- 统一邮箱地址大小写。
- 提取发件人域名、收件人数和退订特征。

`rule_engine.py`

- 从 YAML 加载规则。
- 根据特征增减分数。
- 将分数限制在 0～100。
- 将分数映射为 P1～P5。
- 为每次评分返回明确的 `reason_code`。

### 修改规则配置

如果需要添加可信发件人、修改关键词、调整权重或改变优先级阈值，请阅读：

- [YAML 规则配置指南](docs/rules_configuration.md)

该文档逐项说明了 `config/rules.yaml` 的结构、评分方式、修改注意事项和回归验证流程。

`pipeline.py`

- 串联加载、标准化和规则评分。
- 隔离单封邮件异常。
- 按优先级排序结果。
- 为 CLI、测试和后续 Web 页面提供统一入口。

`evaluation.py`

- 加载并校验独立人工标签。
- 比较优先级、类别和人工复核标记。
- 计算准确率以及 P1 精确率、召回率。
- 输出缺失预测、额外预测和字段不一致明细。

## 阶段一数据流

```text
sample_emails.json
        │
        ▼
   JSON Loader
        │ EmailMessage
        ▼
   Normalizer
        │ NormalizedMessage
        ▼
   Rule Engine
        │ TriageResult
        ▼
 CLI Table / JSON Output
```

计划中的结构化输出示例：

```json
{
  "message_id": "sample-001",
  "priority": "P1",
  "score": 87,
  "category": "academic_deadline",
  "summary": "课程项目需要在周五前提交",
  "reasons": [
    {
      "code": "trusted_sender",
      "description": "发件人在可信联系人名单中",
      "score_change": 30
    },
    {
      "code": "deadline_detected",
      "description": "正文包含明确截止时间",
      "score_change": 20
    }
  ],
  "requires_review": false
}
```

## 开发与质量检查

安装或同步环境：

```powershell
uv sync
```

运行测试：

```powershell
uv run pytest
```

生成覆盖率报告：

```powershell
uv run pytest --cov=src/inbox_agent --cov-report=term-missing
```

运行代码检查：

```powershell
uv run ruff check .
```

运行类型检查：

```powershell
uv run mypy src
```

## 隐私与安全

- 仓库中的演示邮件必须完全虚构并使用 `example.edu` 等保留域名。
- 不得提交真实姓名、学号、邮箱地址、邮件正文或学校内部链接。
- 阶段一不连接 Microsoft 365，不需要邮箱密码或访问令牌。
- 后续 Graph 集成默认使用委托权限并保持只读。
- `.venv`、本地令牌、私有数据和日志不得提交到 Git。
- 群发邮件不一定不重要，教务、考试和紧急安全通知即使群发也应保留较高优先级。

## 阶段一验收标准

- [ ] 新环境执行 `uv sync` 后可以运行项目。
- [x] `inbox-agent demo` 无需邮箱或 API Key 即可运行。
- [x] 至少包含 15 封匿名样例邮件。
- [x] 输入数据经过 Pydantic 校验。
- [x] HTML 正文可以转换为纯文本。
- [x] 规则配置与 Python 代码分离。
- [x] 每个评分变化都有可解释原因。
- [x] 输出支持 P1～P5 和 0～100 分。
- [x] 教务群发邮件不会被简单判定为低优先级。
- [x] 支持终端表格和 JSON 输出。
- [x] pytest、Ruff 和 mypy 全部通过。
- [x] 核心逻辑测试覆盖率达到约 80%。
- [x] 仓库中不存在真实邮件和凭据。

> 2026-08-07 验收结果：当前范围通过，唯一暂缓项为全新环境安装验证。

## 后续路线

完成阶段一后，项目将继续加入：

1. 结构化 LLM 分类和截止日期提取。
2. 规则与 LLM 的置信度路由。
3. Microsoft Graph 委托登录和增量同步。
4. 人工确认与 Outlook 分类写回。
5. 成本统计、Web 演示界面和持续集成。

## License

项目许可证将在首次公开发布前确定。
