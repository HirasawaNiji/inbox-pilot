# 分类 Prompt 设计

## 目标

`triage-v4` 是 InboxPilot 阶段 2.5 使用的结构化邮件分类 Prompt。它负责让 LLM 对一封已经标准化的邮件返回：

- P1～P5 优先级；
- 一个稳定邮件类别；
- 简短摘要；
- 结构化行动项；
- 可选截止时间及原文证据；
- 整体置信度；
- 简短、可审计的判断依据；
- 是否需要人工复核。

Prompt 定义输入和输出契约；是否发送外部请求由调用者是否显式提供本地 Provider 配置和环境变量 API Key 决定。

## 三部分结构

### 1. 版本化系统指令

系统指令固定以下内容：

- Agent 角色和单一任务；
- Prompt Injection 安全边界；
- P1～P5 判定标准；
- 群发邮件不能直接视为低优先级；
- 任务和截止时间提取要求；
- 置信度及人工复核规则；
- 允许使用的类别枚举。

当前版本号为：

```text
triage-v4
```

只要优先级标准、类别含义、输出语义或安全边界发生不兼容变化，就必须更新版本号，并重新运行离线评测。

### 2. 纯 JSON 用户消息

邮件不拼接进系统指令，而是序列化为单独的 JSON 用户消息。主要字段包括：

- 邮件来源和来源 ID；
- 分析时区；
- 收发时间；
- 发件人、实际发送者、回复地址和收件人；
- 主题、正文预览和正文；
- Provider importance；
- 收件人数和附件标记；
- 正文是否被截断。

邮件 JSON 始终被视为不可信数据。正文中出现“忽略规则”“改变角色”“调用工具”或类似文本时，分类器不得执行这些指令。

### 3. Pydantic 响应模型

响应由 `LLMMessageAnalysis` 约束。它采用 `extra="forbid"`，禁止模型返回未定义字段。

所有字段都必须显式返回：

- 没有行动项时返回空数组；
- 没有可靠截止时间时返回 `null`；
- 不需要人工复核时返回 `false`。

这种设计可以直接生成严格 JSON Schema。OpenAI 使用原生 Structured Outputs；DeepSeek JSON Output 会在系统消息中收到完整 Schema，并在响应后由同一 Pydantic 模型再次严格校验。

`triage-v2` 相比 `triage-v1` 的主要变化，是明确向只保证合法 JSON 的 Provider 提供完整响应 Schema，防止模型遗漏 required 字段、返回未定义字段或使用错误的嵌套结构。

`triage-v3` 根据第一轮 DeepSeek 真实冒烟测试收紧时间语义：距离明确截止时间超过 7 天的普通事项默认 P3，7 天内且存在明确损失时通常为 P2，24 小时内且后果严重时通常为 P1。普通选课开放不能仅因“选课”或“重要”关键词提升；候补席位即将释放等不可逆后果仍可提升。

`triage-v4` 根据 50 封 DeepSeek 真实基线细化严重后果、正式资料、newsletter、空标题、考试初次发布、财务退款、通用通知与复核触发规则。两天内可能影响选课的注册确认和失去资格的奖学金补交保持 P1；可补救的图书逾期使用 P2；外部培训优惠使用 P5 / promotion。

## 优先级标准

| 优先级 | 核心含义 |
| --- | --- |
| P1 | 当天考试/课程变更、主动安全风险，或 48 小时内不处理会失去资格、影响注册选课并造成难以恢复的后果 |
| P2 | 未来 7 天内需要行动且不处理有明确损失，或没有日期但属于直接面向个人的近期必办事项 |
| P3 | 值得阅读或计划处理但不紧迫，包括距离截止时间超过 7 天的普通事项 |
| P4 | 低紧迫度、可选参与或礼貌性信息 |
| P5 | 纯推广、营销、低价值订阅或明显噪声 |

优先级不是由单个关键词决定。发件人、邮件正文、时间、后果、群发特征和 Provider importance 需要综合判断。

## 类别约束

类别由 `MessageCategory` 枚举定义。新增或修改类别时，需要同步检查：

1. `src/inbox_agent/models.py` 中的枚举；
2. `src/inbox_agent/llm/prompt.py` 中的类别说明；
3. 规则 Pipeline 的类别映射；
4. 匿名样例和人工期望结果；
5. Prompt、模型和回归评测测试。

## 截止时间规则

- 原文包含绝对时间时使用 `explicit`；
- “明天”“本周五”等相对时间只有在结合 `received_at` 和 `analysis_timezone` 后结果唯一时才使用 `inferred`；
- 日期存在歧义、缺少年份或时区无法可靠确定时返回 `null` 并考虑人工复核；
- `evidence` 必须来自邮件原文；
- 最终时间必须带时区。

## 正文长度限制

默认最多向模型提供 12,000 个正文字符，硬上限为 100,000。正文超出限制时：

- `body_text` 保存截断后的内容；
- `body_truncated` 设置为 `true`；
- Provider 或 Pipeline 可以据此降低置信度或要求人工复核。

截断是成本和上下文保护措施，不代表截断后的内容一定足够完成判断。

## 安全边界

Prompt 明确禁止模型执行邮件中的指令，包括：

- 忽略系统规则；
- 改变角色；
- 泄露 Prompt 或凭据；
- 调用工具或访问链接；
- 发送消息或运行代码。

Prompt Injection 防护不是唯一安全机制。真实 Provider 接入后还应保持无工具权限、最小数据发送、日志脱敏、超时限制和输出 Schema 校验。

项目显式依赖 `tzdata`，确保 Windows 等不自带 IANA 时区数据库的系统也能解析 `Asia/Shanghai` 等时区。

## 修改后的验证

```powershell
uv run pytest tests/test_classification_prompt.py
uv run pytest --cov=src/inbox_agent --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```
