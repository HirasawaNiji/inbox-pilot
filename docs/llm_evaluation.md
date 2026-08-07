# 阶段二 LLM 旁路评测指南

本评测用于验证结构化 LLM 分析的语义结果，包括优先级、类别、摘要、行动项、截止时间和人工复核标记。它完全离线运行，不调用真实模型，也不会改变阶段一规则引擎的最终决策。

## 三类文件的职责

### 匿名邮件：`data/samples/llm_evaluation_emails.json`

这是被分析的原始输入，共 8 封虚构邮件。它们覆盖：

1. “明天 18:00”这类相对截止时间；
2. 一封邮件包含多个行动项；
3. 没有行动要求的简报；
4. 邮件正文包含 Prompt Injection 文本；
5. 无法唯一换算的模糊日期；
6. 标题与正文的截止时间冲突；
7. 转发旧通知与最新截止时间并存；
8. 英文奖学金通知。

该文件只描述邮件事实，不保存期望答案。

### 人工标签：`data/eval/expected_llm_results.json`

这是独立编写的评测标准答案。每封邮件包含：

- `expected_priority`：期望优先级；
- `expected_category`：期望类别；
- `summary_facts`：合格摘要必须覆盖的事实短语；
- `expected_action_phrases`：合格行动项必须覆盖的任务短语；
- `expected_deadline`：期望的绝对时间和 `explicit` / `inferred` 类型；
- `requires_review`：是否必须人工复核；
- `explanation`：人工标注理由。

摘要和行动项采用“关键事实短语包含”而不是全文完全相等，允许模型使用不同但合理的表述。行动项数量必须与人工标签一致。截止时间和类型必须同时匹配；遇到模糊或冲突日期时，期望值为 `null` 并要求人工复核。

### Fake 响应：`data/eval/fake_llm_responses.json`

这是 `FakeLLMProvider` 返回的确定性结构化预测，用于在没有网络、API Key 和模型 SDK 的情况下测试完整接线。每条 `analysis` 都必须通过 `LLMMessageAnalysis` 的严格 Pydantic Schema。

Fake 响应不是人工标签的替代品。前者模拟“系统预测”，后者表示“人工标准答案”，两者分开保存才能检测加载、Pipeline 接线和评测逻辑中的偏差。

## 六项指标

`evaluate_llm_analysis` 以人工标签总数为分母，输出：

- `priority_accuracy`：优先级准确率；
- `category_accuracy`：类别准确率；
- `summary_accuracy`：摘要关键事实覆盖率；
- `action_items_accuracy`：行动项数量和关键短语一致率；
- `deadline_accuracy`：截止时间与显式/推断类型一致率；
- `review_accuracy`：人工复核标记一致率。

缺失预测会计入错误；多余预测会列为 `unexpected_prediction`；Provider 失败数量保存在 `llm_failure_count`。只有所有字段一致且没有 LLM 旁路失败时，`passed` 才为 `true`。

## 本地运行

运行阶段二语义评测测试：

```powershell
uv run pytest tests/test_llm_evaluation.py tests/test_llm_fixtures.py
```

运行完整回归：

```powershell
uv run pytest --cov=src/inbox_agent --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

## 修改数据时的检查顺序

1. 为新邮件分配唯一且稳定的 `source_id`；
2. 在人工标签中新增同一个 `source_id`，先确定事实和期望，再编写 Fake 响应；
3. 在 Fake 响应中提供全部必填字段，不要加入 Schema 以外的字段；
4. 确保三个文件中的 ID 集合完全一致；
5. 运行语义评测和完整回归。

真实 Provider 接入后，可以将其生成的 `LLMAnalysisResult` 放入同一种 `AnalysisReport`，继续使用这套人工标签和评测指标；Fake 数据仍保留为稳定的离线回归基线。
