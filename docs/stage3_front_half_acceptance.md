# 阶段三前半离线验收报告

验收日期：2026-08-08

## 验收范围

阶段三前半包含六个步骤：

1. 不可变动作模型、证据快照、分类白名单与状态机；
2. 本地人工确认队列、批准/拒绝 CLI 和原子 JSON 持久化；
3. 已批准动作的分类差异 dry-run；
4. 隐私受限、确定性去重、追加与 `fsync` 的 JSONL 审计；
5. 稳定幂等键、跨进程文件锁、原子执行认领与安全重试；
6. 单动作、必须说明原因、仅限成功动作的受控回滚 dry-run。

本次验收不包含 Graph 写入、`Mail.ReadWrite` 授权、移动、删除、发送或批量自动批准。

## 安全不变量

验收确认以下边界仍然成立：

- Microsoft Graph 配置只接受 `Mail.Read`；
- 正向 apply 与 rollback 都必须显式使用 `--dry-run`；
- 两类 dry-run 的 `graph_write_request_count` 均由 Schema 固定为 `0`；
- 动作计划只能修改 `InboxPilot/` 分类并保留用户分类；
- 只有人工批准的正向动作可以被执行守卫认领；
- 相同成功动作再次认领会安全 no-op；
- 并发队列与审计更新受操作系统文件锁保护；
- 回滚预览只能针对 `succeeded` 动作并要求用户原因；
- 回滚预览不会改变动作状态，`rolled_back` 只能由未来系统执行器在真实恢复成功后记录；
- 私有队列、审计、Graph 缓存、令牌与真实邮件数据保持在 `data/private/`，不进入 Git。

## 自动化验证命令

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov=inbox_agent --cov-report=term-missing -q
uv run inbox-agent evaluate --format json
```

## 验收结果

完整质量门禁实测结果：

- Ruff 静态检查：通过；
- Ruff 格式检查：75 个文件全部符合格式；
- mypy 严格类型检查：31 个源文件无问题；
- pytest：283 项全部通过；
- 总分支覆盖率：88.43%，高于 80% 门槛。

离线 50 封确定性评估结果：

- 分析失败数：0；
- 优先级准确率：100%；
- 类别准确率：100%；
- 复核准确率：100%；
- 验收结果：PASS。

## 结论

阶段三前半的代码、测试、文档与离线质量门禁全部通过，可以进入阶段三后半设计。提升权限和真实写回仍必须作为独立、显式授权的工作进行，并继续保持小批量、可审计和可回滚边界。
