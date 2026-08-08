# OpenAI / DeepSeek Provider 配置指南

InboxPilot 使用同一个 `OpenAICompatibleProvider` 接入 OpenAI 和 DeepSeek。两种服务共用 OpenAI Python SDK，但结构化输出策略不同：

- OpenAI 使用原生 Structured Outputs，由服务端按 `LLMMessageAnalysis` 的严格 JSON Schema 生成结果；
- DeepSeek 使用兼容 Chat Completions API 的 JSON Object 模式，返回后再由本地 Pydantic 严格校验；
- 两者都只在显式传入 `--llm-config` 时启用，`demo` 和普通 `analyze` 仍可完全离线运行。

## 1. 选择运行方式

InboxPilot 提供两种真实 LLM 运行方式：

- `analyze --llm-config ...`：按 `llm_routing.yaml` 选择邮件，再把模型结果与规则结果安全融合，适合日常分析；
- `validate-llm --llm-config ...`：绕过选择性路由，对公开样例逐封调用模型并和人工标签比较，适合 Prompt 和模型验收。

第一次接入建议先运行 `validate-llm --limit 1`，确认认证、网络、模型 ID 和响应 Schema 均正常，再扩大调用数量。真实 API 调用会产生 Token 消耗和费用。

## 2. 准备本地配置

复制公开示例文件。`.local.yaml` 已被 `.gitignore` 忽略：

```powershell
Copy-Item config/llm_provider.example.yaml config/llm_provider.local.yaml
```

默认示例使用 OpenAI：

```yaml
provider: openai
model: gpt-5.6-luna
base_url: https://api.openai.com/v1
api_key_env: OPENAI_API_KEY

analysis_timezone: Asia/Shanghai
max_body_characters: 12000
max_completion_tokens: 2000
timeout_seconds: 30
max_retries: 2
```

若改用 DeepSeek，将前四项替换为：

```yaml
provider: deepseek
model: deepseek-v4-flash
base_url: https://api.deepseek.com
api_key_env: DEEPSEEK_API_KEY
```

模型名称会随供应商更新。若示例模型不再可用，只需要修改 `model`，不要修改 Provider 代码。

用于 DeepSeek 全量验收时，也可以复制专用模板：

```powershell
Copy-Item config/deepseek_validation.example.yaml `
  config/deepseek_validation.local.yaml
```

## 3. 安全配置 API Key

API Key 不得写入 YAML、JSON、代码、日志或 Git 提交。当前 PowerShell 会话可使用：

```powershell
$env:OPENAI_API_KEY = Read-Host -Prompt "请输入 OpenAI API Key" -MaskInput
```

DeepSeek 使用：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host -Prompt "请输入 DeepSeek API Key" -MaskInput
```

命令执行后，终端会另起一行等待输入；此时再粘贴真实 Key 并按 Enter。不要把真实 Key 写进 `-Prompt` 后面的引号，否则它只会被显示为提示文字，环境变量得到的也不是预期值。

只检查环境变量是否存在，不输出 Key：

```powershell
if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
  "DEEPSEEK_API_KEY 未设置"
} else {
  "DEEPSEEK_API_KEY 已设置"
}
```

确认本地配置不会被 Git 跟踪：

```powershell
git check-ignore -v config/llm_provider.local.yaml
git check-ignore -v config/deepseek_validation.local.yaml
```

完成测试后可以立即从当前 PowerShell 会话清除 Key：

```powershell
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
```

关闭当前终端也会清除会话级环境变量。仓库中的 `.env.example` 只列出变量名；应用不会自动读取 `.env`，以避免开发者误以为密钥已安全加载。如果 Key 曾出现在聊天、截图、终端历史、Issue、PR 或提交中，应立即在供应商控制台撤销并重新生成。

## 4. 运行真实模型分析

先只对仓库中的匿名样例运行：

```powershell
uv run inbox-agent analyze data/samples/sample_emails.json `
  --llm-config config/llm_provider.local.yaml
```

输出完整审计 JSON：

```powershell
uv run inbox-agent analyze data/samples/sample_emails.json `
  --llm-config config/llm_provider.local.yaml `
  --format json
```

默认还会读取：

- `config/llm_routing.yaml`：决定哪些邮件值得调用模型；
- `config/llm_fusion.yaml`：决定模型结果是否可以影响最终判断。

可通过 `--llm-routing-config` 和 `--llm-fusion-config` 指定其他配置。建议保持选择性路由，以减少费用和发送给外部服务的邮件内容。

## 5. 验证模型与 Prompt

先使用 1 封公开虚构邮件做低成本冒烟测试：

```powershell
uv run inbox-agent validate-llm `
  --llm-config config/deepseek_validation.local.yaml `
  --limit 1
```

通过后可先扩大到 5 封，最后再验证全部 50 封：

```powershell
uv run inbox-agent validate-llm `
  --llm-config config/deepseek_validation.local.yaml `
  --limit 5

uv run inbox-agent validate-llm `
  --llm-config config/deepseek_validation.local.yaml
```

报告同时给出精确优先级准确率和显式容差准确率。容差并不是自动把相邻等级都算对：只有人工标签中逐封声明的 `llm_acceptable_priorities` 才会计为可接受差异，并会在报告中单独列出。类别和 `requires_review` 仍按精确值比较。完整门槛与已记录结果见[阶段 2.5 DeepSeek 真实验证指南](stage2_5_deepseek_validation.md)。

`validate-llm` 只应用于仓库中的公开虚构样例。不要把 `--dataset` 指向 `data/private/outlook_inbox.json`；真实邮件会被发送给所选外部 Provider，必须先单独完成隐私审批与脱敏设计。

## 6. 配置字段

| 字段 | 含义 | 限制 |
| --- | --- | --- |
| `provider` | 服务适配策略 | 只能是 `openai` 或 `deepseek` |
| `model` | API 请求使用的模型 ID | 必填，不在 Python 中写死 |
| `base_url` | API 根地址 | 必须是 HTTPS |
| `api_key_env` | 保存密钥的环境变量名称 | 只允许大写字母、数字和下划线 |
| `analysis_timezone` | 解释“明天”等相对日期时使用的时区 | 必须是有效 IANA 时区 |
| `max_body_characters` | 最多发送的标准化正文字符数 | 1～100,000，默认 12,000 |
| `max_completion_tokens` | 单次结构化输出上限 | 64～100,000，默认 2,000 |
| `timeout_seconds` | 单次 HTTP 超时时间 | 大于 0，最大 300 秒 |
| `max_retries` | SDK 自动重试次数 | 0～10 |

所有字段都经过 Pydantic 严格校验；未知字段、HTTP 地址、无效时区和缺失环境变量会在发送请求前失败。

## 7. 两种 Provider 的差异

### OpenAI

- 发送由 Pydantic 模型生成的严格 JSON Schema；
- 使用解析后的 `message.parsed`，拒绝空结果、拒答和非正常结束；
- 发送邮件 ID 的 SHA-256 摘要作为匿名安全标识，不发送原始 ID；
- API Key 通过 Bearer 认证使用；项目只从 `api_key_env` 指定的环境变量读取密钥。

### DeepSeek

- 使用 OpenAI 兼容的 `/chat/completions` 接口；
- 请求 `response_format: {type: json_object}`；
- Prompt 会携带完整 `LLMMessageAnalysis` JSON Schema 并明确要求只返回 JSON；返回后仍使用同一 Pydantic Schema 本地复验；
- 空 JSON、字段缺失、非法枚举或多余字段都会被拒绝，不会静默进入融合阶段。

## 8. 失败处理、退出码与排查顺序

日常 `analyze` 中，单封邮件的 Provider 故障会写入 `llm_failures`，规则结果仍然保留，剩余邮件继续处理。表格模式会显示路由数、跳过数、成功数、融合数和失败数。

验收命令 `validate-llm` 采用 fail-fast：首个 Provider、Schema 或融合错误会停止后续真实调用并显示错误类型与消息，避免同一种配置故障连续消耗额度。验收准确率未达到门槛时，全部已完成的分析仍会进入报告。

| 退出码 | 含义 |
| --- | --- |
| `0` | `analyze` 全部处理成功，或 `validate-llm` 的覆盖率、失败数和准确率均达到门槛 |
| `1` | 数据、标签、规则、Provider 配置或 API Key 环境变量无法加载 |
| `2` | 至少一封邮件发生规则、Provider、响应 Schema 或融合失败 |
| `3` | `validate-llm` 已完成全部调用，但至少一项验收指标低于门槛 |

建议按以下顺序排查：

1. 确认 `api_key_env` 的名字和实际设置的环境变量一致；
2. 确认 `provider`、`model` 和 `base_url` 属于同一供应商；
3. 先运行 `--limit 1`，查看具体错误类型，而不是立即重跑 50 封；
4. 若 Provider 后台已有 Token 消耗但本地显示 Schema 失败，检查返回字段、枚举值和多余字段，不要仅凭调用次数判断成功；
5. 若结构化响应通过但准确率不足，查看逐字段 mismatch 和 accepted variance，再决定调整 Prompt、人工标签解释或融合策略。

## 9. 安全测试建议

1. 第一次只使用匿名样例，不要直接上传真实学校邮件；
2. 使用供应商控制台设置预算或用量限制；
3. 先查看 `--format json` 中的 `llm_routing_decisions`、`llm_analyses` 和 `llm_fusion_decisions`；
4. 确认分类、日期和复核行为符合预期后，再考虑 Microsoft Graph 只读同步；
5. 任何写回 Outlook 的能力都应保持独立授权，不能因配置了 LLM API 而自动开启。

项目测试使用 `httpx.MockTransport` 模拟 OpenAI 和 DeepSeek 响应，不需要真实网络或密钥，并覆盖成功、非法 JSON、空响应、输出截断和 HTTP 故障。

## 官方接口参考

- [OpenAI API Key 与认证](https://platform.openai.com/docs/api-reference/authentication)
- [OpenAI Structured Outputs](https://platform.openai.com/docs/guides/structured-outputs)
- [OpenAI 模型列表 API](https://platform.openai.com/docs/api-reference/models/list)
- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion)
- [DeepSeek JSON Output 指南](https://api-docs.deepseek.com/guides/json_mode/)
