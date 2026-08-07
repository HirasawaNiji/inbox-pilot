# OpenAI / DeepSeek Provider 配置指南

InboxPilot 使用同一个 `OpenAICompatibleProvider` 接入 OpenAI 和 DeepSeek。两种服务共用 OpenAI Python SDK，但结构化输出策略不同：

- OpenAI 使用原生 Structured Outputs，由服务端按 `LLMMessageAnalysis` 的严格 JSON Schema 生成结果；
- DeepSeek 使用兼容 Chat Completions API 的 JSON Object 模式，返回后再由本地 Pydantic 严格校验；
- 两者都只在显式传入 `--llm-config` 时启用，`demo` 和普通 `analyze` 仍可完全离线运行。

## 1. 准备本地配置

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

## 2. 配置 API Key

API Key 不得写入 YAML、JSON、代码、日志或 Git 提交。当前 PowerShell 会话可使用：

```powershell
$env:OPENAI_API_KEY = Read-Host "OpenAI API Key" -MaskInput
```

DeepSeek 使用：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host "DeepSeek API Key" -MaskInput
```

关闭当前终端后，会话级环境变量随之消失。仓库中的 `.env.example` 只列出变量名；应用不会自动读取 `.env`，以避免开发者误以为密钥已安全加载。

## 3. 运行真实模型分析

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

## 4. 配置字段

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

## 5. 两种 Provider 的差异

### OpenAI

- 发送由 Pydantic 模型生成的严格 JSON Schema；
- 使用解析后的 `message.parsed`，拒绝空结果、拒答和非正常结束；
- 发送邮件 ID 的 SHA-256 摘要作为匿名安全标识，不发送原始 ID；
- 适合作为项目第一次真实 API 验证的默认选择。

### DeepSeek

- 使用 OpenAI 兼容的 `/chat/completions` 接口；
- 请求 `response_format: {type: json_object}`；
- Prompt 明确要求 JSON，并在本地使用同一 `LLMMessageAnalysis` Schema 复验；
- 空 JSON、字段缺失、非法枚举或多余字段都会被拒绝，不会静默进入融合阶段。

## 6. 失败处理与退出码

单封邮件的 Provider 故障写入 `llm_failures`，规则结果仍然保留，剩余邮件继续处理。表格模式会显示路由数、跳过数、成功数、融合数和失败数。

- 配置文件无效或 API Key 环境变量缺失：退出码 `1`；
- 已开始批量分析，但至少一次规则或 LLM 分析失败：退出码 `2`；
- 全部成功：退出码 `0`。

## 7. 安全测试建议

1. 第一次只使用匿名样例，不要直接上传真实学校邮件；
2. 使用供应商控制台设置预算或用量限制；
3. 先查看 `--format json` 中的 `llm_routing_decisions`、`llm_analyses` 和 `llm_fusion_decisions`；
4. 确认分类、日期和复核行为符合预期后，再考虑 Microsoft Graph 只读同步；
5. 任何写回 Outlook 的能力都应保持独立授权，不能因配置了 LLM API 而自动开启。

项目测试使用 `httpx.MockTransport` 模拟 OpenAI 和 DeepSeek 响应，不需要真实网络或密钥，并覆盖成功、非法 JSON、空响应、输出截断和 HTTP 故障。

## 官方接口参考

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI 当前模型指南](https://developers.openai.com/api/docs/guides/latest-model)
- [DeepSeek Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion)
- [DeepSeek JSON Output 指南](https://api-docs.deepseek.com/guides/json_mode/)
