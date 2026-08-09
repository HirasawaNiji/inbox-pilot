# 阶段四：本地 Web 控制台

步骤五在既有 FastAPI API 上增加 Jinja2 + HTMX 服务端渲染控制台。控制台面向单用户个人电脑，继续复用步骤四 API 所使用的查询服务和阶段三动作安全层，不在模板路由中重新实现分类、Graph 写回、对账或回滚逻辑。

## 启动与访问

```powershell
uv run inbox-agent db init
uv run inbox-agent web start --port 8765
```

浏览器打开：

- Web 控制台：<http://127.0.0.1:8765/console>
- FastAPI 文档：<http://127.0.0.1:8765/docs>
- 健康检查：<http://127.0.0.1:8765/api/v1/health>

不要绑定到 `0.0.0.0`。当前版本没有公网认证层，只适合本机回环地址。

## 两种退出形式

控制台左侧底部明确区分“转入后台”和“完全退出”：

### 转入后台

点击“转入后台”后可以关闭当前浏览器标签页。此操作不会发送停止信号：

- Web 服务继续监听本机端口，稍后可重新打开控制台；
- 由网页管理的同步和另一个终端中的 `inbox-agent service start` 都会继续同步与分析；
- 因为进程仍在运行，端口保持占用是预期行为。

### 完全退出

点击“完全退出”，检查影响并手工输入精确文本 `EXIT`。表单通过 CSRF 校验后，服务器先返回完成页面，再向受管 Uvicorn 实例发送优雅停止信号。数据库连接在生命周期清理阶段关闭，监听端口随进程退出而释放。

完全退出会先请求由当前网页管理的同步安全停止；如果工作流正在执行，会等待本轮完成，再关闭数据库和 Web 监听端口。独立终端中的调度器不会被网页越权停止；若也要停止它，请在其原终端执行 `Ctrl+C`。

网页安全退出依赖 `inbox-agent web start` 提供的一次性服务器关闭句柄。如果用原始 `uvicorn inbox_agent.web.app:create_app --factory` 启动，页面会禁用退出按钮并提示重新以受管命令启动，而不会尝试按 PID 杀进程。

若端口已被旧进程占用，可以先查看占用者：

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen |
  Select-Object LocalAddress, LocalPort, OwningProcess
Get-Process -Id <OwningProcess>
```

确认它确实是需要停止的旧 InboxPilot Web 进程后，再在原终端结束它。不要直接终止身份不明的 PID。

## 页面

### 收件箱总览

`/console` 展示：

- P1/P2 高优先级邮件数量；
- P1～P5 优先级分布；
- 待人工复核邮件数量；
- 待确认 Outlook 动作数量；
- 最近邮件；
- 最近工作流和调度配置状态。

### 邮件列表与详情

`/console/inbox` 支持优先级、类别和是否需要人工复核筛选。筛选表单由 HTMX 渐进增强，只替换邮件列表区域；HTMX 未加载时仍会退化为普通 GET 表单。

邮件详情明确区分：

- YAML 规则建议、基础分、最终分和逐项命中原因；
- 可选 LLM 的优先级、类别、置信度、理由和 Provider；
- 最终融合优先级、类别、摘要、待办、截止时间和决策来源；
- 规则与 LLM 是否存在优先级冲突；
- 触发人工复核的具体原因。

页面只显示正文预览，不加载远程图片，也不会主动请求邮件中的外部资源。

### 人工复核与动作队列

`/console/reviews` 同时展示分析层待复核邮件和动作层待确认写回，避免把“需要检查分析”误解成“已经批准修改 Outlook”。

`/console/actions` 可以按状态筛选动作。动作详情展示：

- 最终分类理由；
- 当前 Outlook 类别快照；
- 计划管理的 `InboxPilot/` 类别；
- 状态转换历史；
- 当前状态允许的单动作操作。

批准和拒绝均调用 `ActionQueueRepository.transition`，继续使用文件锁并追加既有审计事件。HTMX 请求返回动作面板片段；普通表单提交使用 303 重定向。

## 写回确认流程

控制台不提供批量执行入口。正向写回流程必须依次完成：

1. 查看解释和动作详情；
2. 人工批准动作；
3. 打开零写入预览；
4. 再次检查当前类别、新增类别、移除类别和最终类别；
5. 手工输入完整 Action ID；
6. 由 `ApprovedActionGraphExecutor` 执行实时 GET、冲突检查和最多一次 PATCH。

确认不匹配时，会在读取 Graph 写配置和获取令牌之前返回 `CONFIRMATION_MISMATCH`。

写回结果不确定时，页面只提供 `UncertainActionReconciler` 的一次 GET、零 PATCH 对账入口，不允许盲目重试。

## 受控回滚流程

成功动作可以从详情页提交回滚原因。控制台随后展示：

- 预期当前类别；
- 将恢复的 InboxPilot 类别；
- 将移除的 InboxPilot 类别；
- 回滚后的目标类别；
- 零 Graph 写请求证明；
- 独立回滚幂等键。

用户必须再次输入完整 Action ID 才能调用 `ControlledRollbackExecutor`。执行器会重新读取实时状态，仅恢复 InboxPilot 管理的命名空间，并保留实时存在的用户自有类别。结果不确定时同样只提供零 PATCH 对账。

## 运行状态

`/console/operations` 读取持久化工作流和本地调度状态，展示 Run ID、当前步骤、开始/结束时间、计数、最近错误和工作流步骤。首页与运行状态页都提供受控同步按钮：

- “启动自动同步”读取 `config/service.local.yaml`，复用既有 `ServiceRunner`、SQLite 状态和单实例文件锁；
- 如果另一个终端已经持有锁，网页只显示外部同步状态并拒绝重复启动；
- “停止自动同步”调用 `ServiceRunner.request_stop()`，休眠中的任务立即结束，执行中的工作流完成本轮后结束；
- Web 启动本身不自动启动同步，也不自动调用 LLM。

## LLM 设置

`/console/settings` 提供可选的 OpenAI-compatible LLM 设置。默认状态为关闭，规则引擎不依赖 LLM。用户必须主动提供：

- Provider：OpenAI 或 DeepSeek；
- 与 Provider 联动的模型选项；
- API Key。

当前下拉选项为：

- OpenAI：`gpt-5.6-luna`（默认）、`gpt-5.6-terra`、`gpt-5.6-sol`；
- DeepSeek：`deepseek-v4-flash`、`deepseek-v4-pro`。

OpenAI 选项依据[官方模型目录](https://developers.openai.com/api/docs/models)，DeepSeek 选项是 InboxPilot 当前支持并验证的项目配置。切换 Provider 时页面只保留对应模型，后端还会再次校验组合，不能通过手工请求把 DeepSeek 模型提交给 OpenAI。

第一版采用内存凭据：API Key 通过密码字段和 CSRF 保护的表单提交，只保存在当前 Web 进程内存，不写入 YAML、SQLite、审计或日志，不会在状态页和设置页回显。Web 重启或关闭 LLM 后，内存引用会被清除。下一次启动需重新输入。

OpenAI 固定使用 `https://api.openai.com/v1`，DeepSeek 固定使用 `https://api.deepseek.com`。Web 页面不接受自定义 Base URL，避免用户误把密钥发送到任意主机；需要自定义兼容 Provider 时仍使用原有本地 YAML 与环境变量流程。

启用后仍使用 `config/llm_routing.yaml` 的选择性路由，不会默认把每封邮件交给 LLM。同步运行期间不能修改 Provider、模型或密钥，必须先请求停止同步，防止同一轮工作流中配置漂移。

## 浏览器安全边界

- 所有控制台 POST 表单使用 HttpOnly、SameSite=Strict Cookie 与隐藏表单值的双重 CSRF 校验；
- Jinja2 默认 HTML 转义，邮件主题、发件人、预览和解释结果不能注入页面脚本；
- 响应带 `Content-Security-Policy`、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer` 和 `Cache-Control: no-store`；
- 控制台错误页只展示稳定错误代码、安全消息和 Request ID；
- Token、API Key、Graph 认证信息和完整邮件正文不会进入错误响应；
- 所有写回仍受独立 `graph_write.local.yaml`、`write_enabled` 和委托授权控制。

HTMX 2.0.10 使用带 SRI 哈希的 jsDelivr 地址加载。控制台的导航、筛选和表单均可在 HTMX 不可用时作为普通 HTML 工作，后续部署步骤可以把固定版本资源改为完全本地托管。

## 测试

```powershell
uv run pytest -q -p no:cacheprovider tests/web
```

控制台测试覆盖：

- 缺失数据库时的安全错误页；
- Dashboard、收件箱、解释详情、复核、动作与运行状态页面；
- HTMX 局部筛选与动作面板更新；
- CSRF 拒绝和合法审批；
- 写回前实际类别差异；
- Action ID 精确确认门；
- 成功动作的受控回滚预览；
- 确认失败发生在 Graph 配置读取之前；
- 原有 JSON API 完整回归。
- 后台页面不触发停止回调；
- 完全退出要求 CSRF 和精确 `EXIT`，关闭回调只执行一次；
- 原始 Uvicorn 实例拒绝不安全的网页进程终止。
- LLM 启动默认关闭，密钥不回显且不会写入临时文件；
- 同步按钮经过 CSRF 校验并复用单实例锁；
- 外部调度器运行时拒绝从网页重复启动；
- 同步运行期间拒绝修改 LLM 设置。

真实邮箱验收时应先使用专用测试文件夹。只读浏览不会修改 Outlook；批准/拒绝会改变本地队列并写入审计，`execute` 和 `rollback/execute` 才可能真实修改 Outlook 类别。
