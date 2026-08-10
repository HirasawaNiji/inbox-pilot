# 阶段四步骤一：SQLite 持久化基础

阶段四把 InboxPilot 从一次性 CLI 运行升级为可以长期保存状态的本地 Agent。步骤一只建立
私有数据库和兼容入口，不会自动连接 Outlook、调用 LLM 或写回邮箱。

## 设计目标

- 使用 SQLite 保存邮件、标准化结果、分析结果、人工动作、同步游标和工作流记录；
- 使用 SQLAlchemy 2 提供类型化数据访问；
- 使用 Alembic 管理数据库版本，后续升级不依赖删除旧数据库；
- 通过 `(source, source_id)` 和内容指纹保证重复导入安全；
- 常用筛选字段使用结构化列，完整业务对象使用 JSON 快照无损保存；
- 数据库默认位于 `data/private/`，始终由 Git 忽略。

## 初始数据表

| 表 | 用途 | 关键约束 |
| --- | --- | --- |
| `messages` | 原始邮件和标准化快照 | `(source, source_id)` 唯一 |
| `analyses` | 规则、LLM 与最终分类快照 | 同一邮件和内容指纹唯一 |
| `mailbox_actions` | 最新人工动作状态 | `action_id` 唯一 |
| `sync_cursors` | Outlook 等 Provider 的增量游标 | Provider、邮箱和文件夹作用域唯一 |
| `workflow_runs` | 后续编排器的运行摘要 | `run_id` 唯一 |
| `service_states` | 本地调度器最近状态与退避信息 | `service_name` 唯一 |
| `notification_deliveries` | 通知跨重启去重与有限重试 | `dedupe_key` 唯一 |
| `observability_events` | Run、步骤、Provider 与邮件安全哈希事件 | 按 Run、邮件哈希和 Provider 索引 |

邮件正文等完整数据只存在私有数据库的 JSON 快照中。列表和状态命令只读取计数及索引字段，
不会把正文输出到终端。

## 初始化数据库

在项目根目录运行：

```powershell
uv run inbox-agent db init
```

默认数据库路径为：

```text
data/private/inbox_pilot.sqlite3
```

重复执行 `db init` 是安全的。Alembic 只应用尚未执行的迁移。

查看版本和计数：

```powershell
uv run inbox-agent db status
uv run inbox-agent db status --format json
```

`db status` 在数据库不存在时不会创建空文件。

## 导入现有 JSON 邮件

导入项目的 50 封匿名样例：

```powershell
uv run inbox-agent db import-json data/samples/sample_emails.json
```

导入时会：

1. 校验现有 `MessageDataset` JSON Schema；
2. 自动把数据库升级到最新版本；
3. 按 Provider 和 `source_id` 幂等写入邮件；
4. 运行现有 HTML 清理与标准化逻辑；
5. 保存原始模型和标准化模型的无损 JSON 快照；
6. 输出新增、更新、未变化和标准化数量。

第二次导入相同文件时，邮件应全部显示为 `unchanged`，数据库仍只有 50 条邮件记录。

也可以指定独立测试数据库：

```powershell
uv run inbox-agent db import-json data/samples/sample_emails.json `
  --database data/private/test.sqlite3 `
  --format json
```

## Repository 边界

业务代码不直接拼接 SQL，而是通过以下 Repository 操作数据库：

- `MessageRepository`：邮件和标准化快照；
- `AnalysisRepository`：不可变分类结果；
- `MailboxActionRepository`：人工动作最新状态；
- `SyncCursorRepository`：增量同步游标。

所有写入均在显式事务中完成；异常会自动回滚。SQLite 连接始终启用外键约束和忙等待超时。

## 隐私和 Git 安全

`.gitignore` 已覆盖：

- `*.db`；
- `*.sqlite`；
- `*.sqlite3`；
- 整个 `data/private/`。

因此默认数据库不会被提交到 GitHub。提交前仍建议运行：

```powershell
git status --short
git check-ignore data/private/inbox_pilot.sqlite3
```

第二条命令应输出数据库路径。

## 步骤一验收

```powershell
uv run inbox-agent db status --format json
uv run inbox-agent db import-json data/samples/sample_emails.json --format json
uv run inbox-agent db import-json data/samples/sample_emails.json --format json
uv run inbox-agent db status --format json
```

预期结果：

- 当前 Revision 为 `0005_observability`（旧数据库会由 Alembic 自动升级）；
- 第一次导入 `created` 为 50；
- 第二次导入 `unchanged` 为 50；
- 最终 `messages` 为 50；
- `data/private/inbox_pilot.sqlite3` 不出现在 Git 待提交文件中。

步骤二的工作流编排器会复用这些表和 Repository，把 Outlook 同步、规则分析、LLM 分析和
动作建议串成可重复运行的流水线。
