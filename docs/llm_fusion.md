# 规则与 LLM 受控融合指南

`LLMFusionEngine` 将确定性规则结果与结构化 LLM 分析组合成最终 `TriageResult`。设计目标不是让模型覆盖规则，而是在安全边界内补充语义字段，并把所有不确定情况交给人工复核。

默认配置位于：

```text
config/llm_fusion.yaml
```

## 默认安全原则

1. 规则分数始终保留，LLM 不直接生成或修改 `score`；
2. 只有达到最低置信度的 LLM 结果才能提供类别、摘要、行动项和截止时间；
3. 默认允许把优先级提升到更紧急等级；
4. 默认禁止 LLM 降低规则优先级；
5. 规则与 LLM 优先级不同会强制人工复核；
6. 截止时间冲突时采用较早时间，并强制人工复核；
7. 规则、路由、Provider 或融合任一环节失败时，原规则结果仍然可用。

这意味着 LLM 可以把疑似紧急邮件从 P3 提升到 P1，但不能静默把规则认定的 P1 降成 P4。

## 配置结构

```yaml
fusion_version: llm-fusion-v1
mode: conservative
minimum_llm_confidence: 0.80

allow_priority_upgrade: true
allow_priority_downgrade: false
force_review_on_priority_disagreement: true
force_review_on_deadline_conflict: true
deadline_tolerance_minutes: 0
```

### `fusion_version`

融合策略版本会写入 `LLMFusionDecision`。修改阈值或安全开关后建议升级版本，例如 `llm-fusion-v1.1`。

### `mode`

| 值 | 行为 | 用途 |
| --- | --- | --- |
| `conservative` | 应用默认保守融合算法 | 实际 Pipeline |
| `sidecar_only` | 保存 LLM 分析，但完全不修改规则结果 | 独立 LLM 语义评测 |

语义评测必须使用 `sidecar_only`，否则融合后的公共结果会混入对模型自身的评测。

### `minimum_llm_confidence`

LLM 的整体 `confidence` 必须大于等于该值，才会采用其结构化字段。低于阈值时：

- 保留规则优先级、类别、摘要、行动项和截止时间；
- 最终 `decision_source` 设为 `hybrid`，因为模型置信度参与了判断；
- 强制 `requires_review=true`；
- 记录 `llm_below_confidence`。

### 优先级安全开关

```yaml
allow_priority_upgrade: true
allow_priority_downgrade: false
force_review_on_priority_disagreement: true
```

默认行为：

| 规则结果 | LLM 结果 | 最终优先级 | 是否复核 |
| --- | --- | --- | --- |
| P3 | P3 | P3 | 继承双方复核标记 |
| P3 | P1 | P1 | 是 |
| P1 | P4 | P1 | 是，且记录降级被阻止 |

即使显式开启 `allow_priority_downgrade`，只要 `force_review_on_priority_disagreement=true`，降级结果仍会要求人工确认。

`score` 始终是规则分数。因此当 LLM 把 P3 提升为 P1 时，最终优先级和规则分数可能不再对应 YAML 阈值；`LLMFusionDecision` 会明确记录这是一次 LLM 升级，而不是规则评分变化。

### 截止时间安全开关

```yaml
force_review_on_deadline_conflict: true
deadline_tolerance_minutes: 0
```

截止时间融合规则：

- 规则没有、LLM 有：采用 LLM 截止时间；
- 规则有、LLM 没有：保留规则截止时间；
- 两者差异不超过容差：视为一致，保守保留较早时间；
- 两者差异超过容差：视为冲突，采用较早时间并按配置强制复核。

容差范围为 `0～1440` 分钟。默认 `0` 要求两个绝对时间完全一致。

## 融合后的字段来源

高置信度 LLM 分析进入保守融合后：

| `TriageResult` 字段 | 来源 |
| --- | --- |
| `score`、`reasons`、`policy_version` | 始终来自规则引擎 |
| `priority` | 按优先级安全策略决定 |
| `category`、`summary`、`action_items` | 来自高置信度 LLM |
| `deadline` | 按截止时间安全策略决定 |
| `confidence` | 规则与 LLM 置信度中的较低值 |
| `requires_review` | 任一来源要求复核或发生安全冲突时为 `true` |
| `decision_source` | 应用融合时为 `hybrid` |

## 原因代码

常用 `LLMFusionReasonCode` 包括：

- `structured_fields_adopted`：采用 LLM 语义字段；
- `priority_agreement`：双方优先级一致；
- `llm_priority_upgrade`：LLM 提升优先级；
- `llm_priority_downgrade_blocked`：安全策略阻止降级；
- `priority_disagreement_review`：优先级冲突强制复核；
- `llm_below_confidence`：LLM 未达到融合阈值；
- `llm_review_requested`：LLM 主动要求复核；
- `deadline_added`：规则无日期，采用 LLM 日期；
- `deadline_agreement`：双方日期一致；
- `deadline_conflict`：日期冲突，采用较早日期；
- `sidecar_only`：评测模式未修改规则结果。

## Pipeline 接入

同时加载规则、路由和融合策略：

```python
from inbox_agent.pipeline import OfflinePipeline

pipeline = OfflinePipeline.from_yaml(
    "config/rules.yaml",
    llm_provider=provider,
    llm_routing_path="config/llm_routing.yaml",
    llm_fusion_path="config/llm_fusion.yaml",
)
```

独立评测模式：

```python
from inbox_agent.llm import LLMFusionEngine, LLMRouter

pipeline = OfflinePipeline.from_yaml(
    "config/rules.yaml",
    llm_provider=fake_provider,
    llm_router=LLMRouter.analyze_all(),
    llm_fusion=LLMFusionEngine.sidecar_only(),
)
```

不能同时提供 `llm_fusion` 和 `llm_fusion_path`。

## 查看融合结果

融合记录位于：

```python
report.llm_fusion_decisions
```

汇总属性：

```python
report.llm_fused_count
report.llm_sidecar_only_count
```

每条决策保存规则、LLM 和最终优先级，两侧置信度、最终复核状态、策略版本及全部融合原因。

## 修改后的验证

```powershell
uv run pytest tests/test_llm_fusion.py tests/test_pipeline.py
uv run inbox-agent evaluate
uv run pytest --cov=src/inbox_agent --cov-report=term-missing
uv run ruff check .
uv run mypy src
```

修改安全开关时，应重点验证两类风险：高优先级邮件是否可能被错误降级，以及截止时间冲突是否仍然会进入人工复核。
