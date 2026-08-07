# LLM 调用路由配置指南

InboxPilot 不需要把每封邮件都发送给 LLM。`LLMRouter` 会先检查规则结果的置信度、人工复核标记和确定性冲突信号，只把规则难以可靠处理的邮件交给 Provider。

默认配置位于：

```text
config/llm_routing.yaml
```

路由本身只决定“是否调用 LLM”，不会直接修改规则分数或优先级。跳过 Provider 的邮件保持 `decision_source=rule`；调用成功后，后续 `LLMFusionEngine` 会按独立安全策略决定是否生成 `decision_source=hybrid` 的融合结果。

## 配置结构

```yaml
routing_version: llm-routing-v1
mode: selective
confidence_threshold: 0.75

route_on_rule_review: true
route_on_ambiguous_deadline: true
route_on_multiple_deadlines: true
route_on_action_no_action_conflict: true
route_on_importance_content_conflict: true
route_on_urgent_no_action_conflict: true
```

### `routing_version`

路由策略版本会写入每一条 `LLMRoutingDecision`，用于追踪一次 Provider 调用由哪版策略触发。修改阈值或开关后建议升级版本，例如 `llm-routing-v1.1`。

### `mode`

支持两种模式：

| 值 | 行为 | 用途 |
| --- | --- | --- |
| `selective` | 只分析低置信度或存在冲突的邮件 | 默认运行模式，减少成本和数据暴露 |
| `all` | 每封规则处理成功的邮件都调用 Provider | 离线评测、基准测试和调试 |

阶段二语义评测显式使用 `LLMRouter.analyze_all()`，确保 8 封测试邮件都有预测。日常 Pipeline 默认使用 `selective`。

### `confidence_threshold`

```yaml
confidence_threshold: 0.75
```

当规则结果的 `confidence` 严格小于该值时，路由到 LLM。取值范围为 `0.0～1.0`。

当前规则 Pipeline 对普通确定结果给出 `0.9`，对要求复核的结果给出 `0.6`，因此默认阈值能够区分这两类邮件。提高阈值会增加 LLM 调用，降低阈值会减少调用。

## 冲突开关

所有开关都可以单独关闭。关闭只会禁止对应信号触发调用，不会改变规则引擎本身。

| 配置项 | 原因代码 | 触发条件 |
| --- | --- | --- |
| `route_on_rule_review` | `rule_requires_review` | 规则结果已标记 `requires_review=true` |
| `route_on_ambiguous_deadline` | `ambiguous_deadline` | 有截止语言，但没有可解析日期 |
| `route_on_multiple_deadlines` | `multiple_deadlines` | 检测到两个或更多候选截止时间 |
| `route_on_action_no_action_conflict` | `action_no_action_conflict` | 同时出现行动词和无需行动表述 |
| `route_on_importance_content_conflict` | `importance_content_conflict` | 高重要性标记与推广、退订或无需行动内容并存 |
| `route_on_urgent_no_action_conflict` | `urgent_no_action_conflict` | 紧急/安全词与无需行动表述并存 |

如果选择性模式下没有任何触发条件，决策会记录 `high_confidence_rule`，并跳过 Provider。全量模式统一记录 `full_evaluation`。

一封邮件可以同时包含多个路由原因。例如活动报名邮件可能同时触发：

```text
low_rule_confidence
rule_requires_review
action_no_action_conflict
```

保留全部原因有助于解释成本、排查误调用并调整配置。

## Pipeline 接入

从 YAML 加载规则和路由配置：

```python
from inbox_agent.pipeline import OfflinePipeline

pipeline = OfflinePipeline.from_yaml(
    "config/rules.yaml",
    llm_provider=provider,
    llm_routing_path="config/llm_routing.yaml",
)
report = pipeline.analyze_file("data/samples/sample_emails.json")
```

也可以直接传入已经构造的路由器：

```python
from inbox_agent.llm import LLMRouter

pipeline = OfflinePipeline.from_yaml(
    "config/rules.yaml",
    llm_provider=provider,
    llm_router=LLMRouter.analyze_all(),
)
```

`llm_router` 和 `llm_routing_path` 不能同时提供。只传 Provider 时，Pipeline 会使用代码中的默认选择性策略，其参数与仓库当前 YAML 默认值一致。

## 查看路由结果

每封进入路由判断的邮件都会产生 `LLMRoutingDecision`，保存在：

```python
report.llm_routing_decisions
```

关键字段：

- `message_id`：邮件来源 ID；
- `should_analyze`：是否调用 Provider；
- `rule_confidence`：路由时看到的规则置信度；
- `routing_version`：路由配置版本；
- `reasons`：稳定原因代码、中文说明和可选证据。

汇总属性：

```python
report.llm_routed_count
report.llm_skipped_count
report.llm_analysis_count
report.llm_failure_count
```

路由或 Provider 失败会写入 `llm_failures`，但规则结果仍然保留，不会中止整批分析。

## 调整建议

1. 先查看真实路由决策中的原因分布；
2. 如果调用过多，确认是低置信度还是某项冲突开关导致；
3. 优先修正规则关键词或关闭误报严重的单项开关；
4. 最后再调整全局 `confidence_threshold`；
5. 修改后运行阶段一回归、路由测试和 LLM 语义评测。

```powershell
uv run inbox-agent evaluate
uv run pytest tests/test_llm_routing.py tests/test_pipeline.py tests/test_llm_evaluation.py
uv run pytest --cov=src/inbox_agent --cov-report=term-missing
```

不要为了减少调用而关闭所有冲突信号。模糊日期、冲突日期和高重要性推广等案例正是 LLM 旁路最有价值的地方。
