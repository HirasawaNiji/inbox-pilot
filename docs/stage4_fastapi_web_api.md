# 阶段四：本地 FastAPI Web API

本步骤为后续 Jinja2 + HTMX 控制台提供一个仅限本机访问的 HTTP 接口。API 只负责参数校验、调用现有业务层和序列化响应，不重新实现邮件分类、审批、Graph 写回、对账或回滚逻辑。

## 启动

先初始化或升级数据库，然后只监听回环地址：

```powershell
uv run inbox-agent db init
uv run uvicorn inbox_agent.web.app:create_app `
  --factory `
  --host 127.0.0.1 `
  --port 8765
```

启动后可访问：

- API 文档：<http://127.0.0.1:8765/docs>
- OpenAPI Schema：<http://127.0.0.1:8765/openapi.json>
- 健康检查：<http://127.0.0.1:8765/api/v1/health>

不要把服务绑定到 `0.0.0.0`。当前版本面向单用户本地控制台，没有提供公网认证层；应用也通过 Trusted Host 中间件只接受 `localhost` 和回环地址。

## 数据来源与业务层复用

读取接口直接查询阶段四 SQLite 数据库：

- `messages`、最新分析结果和工作流运行记录来自 SQLAlchemy Repository 所使用的真实数据库；
- 数据库不存在或迁移版本落后时，读取接口返回安全的 `503`，API 启动本身不会创建或迁移数据库；
- 邮件详情返回规则结果、可选 LLM 结果和最终融合结果，供 Web 控制台解释分类原因。

动作接口继续使用阶段三的操作状态与安全组件：

- `ActionQueueRepository` 负责文件锁和动作状态转换；
- `ActionAuditLog` 记录审批、拒绝、预览、执行、对账和回滚；
- `ApprovedActionGraphExecutor` 负责写前检查和单动作 Graph 写入；
- 正向与回滚 Reconciler 只做读取对账，不重复 PATCH；
- `ControlledRollbackExecutor` 只恢复 InboxPilot 管理的类别，并保留用户自有类别。

因此 Web API 不存在一条绕过 CLI 既有锁、审计、幂等键、实时冲突检查或显式确认门的写入路径。

## 第一批接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/v1/health` | 数据库版本、可用性和记录计数 |
| `GET` | `/api/v1/messages` | 邮件分页，支持 `priority`、`category`、`requires_review` 筛选 |
| `GET` | `/api/v1/messages/{database_id}` | 邮件、标准化结果、规则、LLM 与最终分析详情 |
| `GET` | `/api/v1/reviews` | 待人工复核邮件 |
| `GET` | `/api/v1/actions` | 动作列表，可按状态筛选 |
| `GET` | `/api/v1/actions/{action_id}` | 单个动作详情 |
| `POST` | `/api/v1/actions/{action_id}/approve` | 批准动作 |
| `POST` | `/api/v1/actions/{action_id}/reject` | 拒绝动作 |
| `POST` | `/api/v1/actions/{action_id}/preview` | 生成零写入预览 |
| `POST` | `/api/v1/actions/{action_id}/execute` | 显式确认后执行单动作写回 |
| `POST` | `/api/v1/actions/{action_id}/reconcile` | 对账结果不确定的正向写回 |
| `POST` | `/api/v1/actions/{action_id}/rollback/preview` | 预览受控回滚 |
| `POST` | `/api/v1/actions/{action_id}/rollback/execute` | 显式确认后执行受控回滚 |
| `POST` | `/api/v1/actions/{action_id}/rollback/reconcile` | 对账结果不确定的回滚 |
| `GET` | `/api/v1/workflows/runs/latest` | 最近一次持久化工作流状态 |
| `GET` | `/api/v1/workflows/runs/{run_id}` | 指定工作流状态与步骤 |
| `GET` | `/api/v1/service/status` | 本地调度服务的锁和状态信息 |

## 危险操作确认

正向写回和回滚执行都要求请求体中的 `confirm_action_id` 与 URL 中的动作 ID 完全一致。确认失败发生在读取 Graph 写配置或获取令牌之前。

正向写回请求示例：

```json
{
  "confirm_action_id": "复制自待执行动作的完整 Action ID",
  "idempotency_key": "动作中记录的 64 位小写十六进制幂等键"
}
```

回滚执行请求示例：

```json
{
  "reason": "分类结果不符合预期",
  "confirm_action_id": "复制自已执行动作的完整 Action ID",
  "rollback_idempotency_key": "回滚预览生成的 64 位小写十六进制幂等键"
}
```

动作必须先进入允许的状态。Graph 写配置仍需显式启用 `write_enabled`，并使用隔离的 `Mail.ReadWrite` 委托授权。API 没有批量执行入口。

## 路径覆盖

默认路径可通过环境变量覆盖，便于使用隔离测试数据：

| 环境变量 | 默认值 |
| --- | --- |
| `INBOX_PILOT_PROJECT_ROOT` | 项目根目录 |
| `INBOX_PILOT_DATABASE_PATH` | `data/private/inbox_pilot.sqlite3` |
| `INBOX_PILOT_ACTION_QUEUE_PATH` | `data/private/action_queue.json` |
| `INBOX_PILOT_AUDIT_LOG_PATH` | `data/private/action_audit.jsonl` |
| `INBOX_PILOT_GRAPH_WRITE_CONFIG_PATH` | `config/graph_write.local.yaml` |
| `INBOX_PILOT_SERVICE_CONFIG_PATH` | `config/service.local.yaml` |
| `INBOX_PILOT_SERVICE_NAME` | `inbox-pilot` |

相对路径均相对于 `INBOX_PILOT_PROJECT_ROOT` 解析。私有配置、令牌缓存、真实邮件、数据库和审计日志仍应位于 Git 忽略路径。

## 错误与隐私

- 所有响应均带 `X-Request-ID` 和 `Cache-Control: no-store`；
- 参数校验错误只返回通用消息，不回显请求体中的 Token、API Key 或确认值；
- 业务错误映射为稳定的错误代码和安全消息；
- 未处理异常不把内部异常、路径、邮件正文或密钥返回给客户端；
- API 不启用 CORS，也不自动迁移数据库或启动后台调度器。

## 验收

运行步骤四 API 测试：

```powershell
uv run pytest -q -p no:cacheprovider tests/web
```

测试覆盖文档页面、OpenAPI 路由契约、缺失数据库启动、真实 SQLite 查询、解释信息、队列锁与审计、零写入预览、显式确认门和敏感请求值不回显。

对真实本地数据只做读取冒烟验证时，可以启动 API 后访问健康检查和邮件列表。不要在未准备独立测试邮箱、写配置和明确确认之前调用 `execute` 或 `rollback/execute`。
