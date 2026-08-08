# 阶段 2.5：50 封样例与 DeepSeek 真实验证

阶段 2.5 是阶段二“智能分析与 Outlook 只读同步”和阶段三“人工确认与受控写回”之间的过渡阶段。它扩大公开回归数据，并用真实 DeepSeek API 验证结构化分类能力、稳定性、耗时和 Token 用量。

## 目标与边界

- 主 demo 从 20 封扩充到 50 封完全虚构的邮件；
- 人工标签与规则、Prompt 和模型输出分开维护；
- `validate-llm` 默认对全部 50 封调用指定 Provider，不使用选择性路由；冒烟测试可用 `--limit` 限制调用数量；
- 指标直接比较原始 LLM 输出，不使用融合后的结果冒充模型成绩；
- 只发送仓库中的公开虚构邮件，不发送 `data/private/` 中的 Outlook 邮件；
- API Key 只从环境变量读取；
- 本阶段不申请 Outlook 写权限，也不修改真实邮箱。

新增 30 封邮件覆盖安全、考试、宿舍行政、实习、推广、课程变更、图书、缴费、奖学金、MFA、问卷、成绩单、实验室培训、钓鱼演练、多截止时间、无标题、招聘汇总、作业、系统维护、退款、志愿活动、配额告警、导师确认、候补选课和极端天气。数据同时包含中英文、HTML、附件、群发、外部发件人、sender/from 不一致和 Prompt Injection 文本。

## 验收指标

`validate-llm` 使用 `data/eval/expected_results.json` 中独立维护的 50 个人工标签，报告以下指标：

| 指标 | 最低门槛 |
| --- | ---: |
| 分析覆盖率 | 100% |
| Provider / Schema 失败 | 0 |
| 优先级容差准确率 | 80% |
| 类别准确率 | 80% |
| 人工复核准确率 | 80% |
| 三个字段容差一致率 | 70% |

报告同时保留优先级精确准确率和三个字段完全精确率。规则回归继续使用 `expected_priority` 和 `expected_category`；真实模型验证可以通过 `llm_expected_priority`、`llm_expected_category` 记录经人工确认的模型验收标准。只有逐封声明的 `llm_acceptable_priorities` 才能计入容差，且容差命中会单独列出，不会被隐藏。报告还包含输入 Token、输出 Token、缓存命中 Token、累计耗时和逐字段不一致明细。价格可能变化，因此项目记录 Token，不在代码中硬编码费用；计算成本时应查看 DeepSeek 当日官方价格。

## 准备 DeepSeek 配置

安装项目依赖：

```powershell
Set-Location inbox-pilot
uv sync --locked
```

复制公开模板：

```powershell
Copy-Item config/deepseek_validation.example.yaml `
  config/deepseek_validation.local.yaml
```

模板默认使用：

```yaml
provider: deepseek
model: deepseek-v4-flash
base_url: https://api.deepseek.com
api_key_env: DEEPSEEK_API_KEY
```

项目使用 DeepSeek JSON Output，并在本地通过严格 Pydantic Schema 复验。分类请求显式关闭 thinking 模式，以降低延迟和 Token 消耗。模型名称可能随 DeepSeek API 更新；运行前可调用官方 `/models` 接口或查阅官方模型列表。

## 安全设置 API Key

不要把 Key 写入 YAML。仅在当前 PowerShell 会话中设置：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host "DeepSeek API Key" -MaskInput
```

确认本地配置被 Git 忽略：

```powershell
git check-ignore -v config/deepseek_validation.local.yaml
```

不要把 API Key、请求响应或真实 Outlook 数据复制到 Issue、PR、日志和聊天记录。

## 执行真实验证

先只调用 1 封，确认连接、Prompt 和响应 Schema：

```powershell
uv run inbox-agent validate-llm `
  --llm-config config/deepseek_validation.local.yaml `
  --limit 1
```

通过后调用 5 封，观察初步一致性和 Token：

```powershell
uv run inbox-agent validate-llm `
  --llm-config config/deepseek_validation.local.yaml `
  --limit 5
```

最后执行 50 封完整表格报告：

```powershell
uv run inbox-agent validate-llm `
  --llm-config config/deepseek_validation.local.yaml
```

机器可读报告：

```powershell
uv run inbox-agent validate-llm `
  --llm-config config/deepseek_validation.local.yaml `
  --format json
```

DeepSeek 请求包含 `LLMMessageAnalysis` 的完整 JSON Schema，返回值仍会由同一 Pydantic 模型在本地严格复验。命令会产生 API 费用；发生首个 Provider、Schema 或融合错误后，会保留具体错误并停止后续 LLM 调用，避免相同故障连续消耗额度。不要把 `--dataset` 改为 `data/private/outlook_inbox.json`；真实邮箱泛化测试必须另行设计隐私审批和脱敏流程。

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 覆盖率、失败数和四项准确率全部达到门槛 |
| `1` | 数据、标签、规则、Provider 或 API Key 配置无法加载 |
| `2` | 至少一封邮件发生 Provider、响应 Schema 或融合失败 |
| `3` | 全部调用完成，但至少一项准确率低于门槛 |

## 如何解释结果

真实模型验证不要求 100%。优先查看：

1. 是否出现空 JSON、截断或 Schema 失败；
2. P1 是否被降为低优先级；
3. 推广邮件是否被标题中的“重要”或 Prompt Injection 提升；
4. 多截止时间和无标题邮件是否要求人工复核；
5. 中英文邮件是否出现明显差异；
6. 同一 commit、模型和 Prompt 重复运行时结果是否稳定。

模型输出变化时，不要直接修改人工标签。先检查标签解释、Prompt 契约和模型不一致明细，再决定是修 Prompt、调整融合边界，还是将案例保留为已知限制。

## 已记录的真实冒烟测试

2026-08-08 使用 `deepseek-v4-flash` 和 `triage-v2` 对第一封公开样例执行 `--limit 1`：

- 结构化分析覆盖率 100%，Provider / Schema 失败为 0；
- 类别与人工复核标记一致；
- 模型将两周后截止的普通选课通知判为 P2，人工标签与规则基线均为 P3；
- 输入 1,897 Token、输出 311 Token、缓存命中 768 Token，耗时 2.73 秒。

该结果没有修改人工标签，而是推动 Prompt 升级到 `triage-v3`：明确超过 7 天的普通截止事项默认 P3，并保留临近截止、资格即将丢失和其他不可逆后果的升级通道。

随后使用 `deepseek-v4-flash` 和 `triage-v3` 完成 50 封全量基线：

- 50/50 成功，Provider / Schema 失败为 0；
- 优先级准确率 62%，类别准确率 80%，人工复核准确率 90%，完全一致率 42%；
- 输入 99,621 Token、输出 10,636 Token、缓存命中 92,288 Token，累计耗时 106.80 秒；
- 明确错误集中在当天课程变更、安全警报、newsletter、空标题、类别边界和复核触发；
- 相邻 P2/P3/P4 边界中存在合理分歧，因此后续同时报告精确指标和显式容差指标。

人工确认两天内注册确认和失去奖学金资格的补交均为 P1，可补救的图书逾期为 P2，外部培训优惠为 P5 / promotion。这些结论与其余明确错误共同推动 Prompt 升级到 `triage-v4`。

随后于 2026-08-08 完成 `triage-v4` 本地复测。用户确认在逐封声明的优先级容差口径下准确率达到 80%，并将其接受为阶段 2.5 的基本验收结果。因此，50 封公开样例、结构化响应契约、显式容差报告和安全融合可以作为阶段三的起始基线。

本轮没有把完整机器可读报告提交到仓库，因此本文不补写未经确认的精确优先级、类别、人工复核、Token 和耗时数值。后续复测应按照下节格式记录全部分项，避免只用单一总体准确率掩盖 P1、安全通知或推广分类等关键错误。

## 推荐记录

每次真实验证至少记录：

- Git commit SHA；
- 日期和时区；
- Provider 与模型 ID；
- Prompt 版本；
- 数据集版本；
- 四项准确率和覆盖率；
- Provider 失败数；
- 输入、输出和缓存 Token；
- 累计耗时；
- 不一致案例及人工结论。

验证报告可能包含公开样例 ID，但不应包含 API Key。若需要保留本地完整 JSON，可将它写入 `data/private/`，该目录已被 Git 忽略。

## 阶段三关系

阶段三前半部分将实现人工确认队列、dry-run、审计日志、幂等与回滚设计。只有这些安全机制通过验收后，阶段三后半部分才考虑申请 `Mail.ReadWrite` 并写回 Outlook 分类。

官方参考：

- [DeepSeek 模型列表](https://api-docs.deepseek.com/api/list-models)
- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)
- [DeepSeek Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion)
- [DeepSeek 模型与价格](https://api-docs.deepseek.com/quick_start/pricing)
