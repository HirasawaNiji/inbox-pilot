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

待完成：

- [ ] 定义原始邮件、标准化邮件和分类结果模型
- [ ] 实现 JSON 邮件加载器
- [ ] 实现 HTML 清理与邮件标准化
- [ ] 实现 YAML 驱动的可解释规则引擎
- [ ] 准备匿名样例邮件
- [ ] 实现离线分析流水线和 CLI
- [ ] 编写单元测试
- [ ] 配置 Ruff、mypy 和 pytest
- [ ] 达到阶段一验收标准

> 当前已完成的是阶段一的工程初始化部分，阶段一完整功能仍在开发中。

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
│       ├── cli.py            # demo、analyze 等 CLI 命令
│       ├── models.py         # 邮件、评分原因和分类结果模型
│       ├── loader.py         # 从 JSON 加载并校验邮件
│       ├── normalizer.py     # HTML 清理、空白处理和字段标准化
│       ├── rule_engine.py    # 可解释的规则评分引擎
│       └── pipeline.py       # 组织加载、标准化、评分和输出
├── data/
│   └── samples/
│       └── sample_emails.json # 虚构且匿名的演示邮件
├── config/
│   └── rules.yaml            # 白名单、关键词、权重和阈值
├── tests/
│   ├── test_loader.py
│   ├── test_normalizer.py
│   ├── test_rule_engine.py
│   └── test_pipeline.py
├── docs/                     # 架构、安全和评测文档
├── pyproject.toml            # 项目配置与直接依赖
├── uv.lock                   # 可复现依赖锁文件
├── README.md
└── .gitignore
```

### 模块职责

`models.py`

- `RawMessage`：对应 JSON 或未来 Graph API 返回的原始邮件。
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

`pipeline.py`

- 串联加载、标准化和规则评分。
- 隔离单封邮件异常。
- 按优先级排序结果。
- 为 CLI、测试和后续 Web 页面提供统一入口。

## 阶段一数据流

```text
sample_emails.json
        │
        ▼
   JSON Loader
        │ RawMessage
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
- [ ] `inbox-agent demo` 无需邮箱或 API Key 即可运行。
- [ ] 至少包含 15 封匿名样例邮件。
- [ ] 输入数据经过 Pydantic 校验。
- [ ] HTML 正文可以转换为纯文本。
- [ ] 规则配置与 Python 代码分离。
- [ ] 每个评分变化都有可解释原因。
- [ ] 输出支持 P1～P5 和 0～100 分。
- [ ] 教务群发邮件不会被简单判定为低优先级。
- [ ] 支持终端表格和 JSON 输出。
- [ ] pytest、Ruff 和 mypy 全部通过。
- [ ] 核心逻辑测试覆盖率达到约 80%。
- [ ] 仓库中不存在真实邮件和凭据。

## 后续路线

完成阶段一后，项目将继续加入：

1. 结构化 LLM 分类和截止日期提取。
2. 规则与 LLM 的置信度路由。
3. Microsoft Graph 委托登录和增量同步。
4. 人工确认与 Outlook 分类写回。
5. 离线评测、成本统计和演示界面。

## License

项目许可证将在首次公开发布前确定。
