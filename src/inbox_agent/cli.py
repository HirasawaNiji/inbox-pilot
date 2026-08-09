"""Command-line interface for offline rules and optional LLM analysis."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import httpx
import typer
from alembic.util.exc import CommandError as AlembicCommandError
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from sqlalchemy.exc import SQLAlchemyError

from inbox_agent.actions import (
    ActionActor,
    ActionAuditLog,
    ActionAuditStorageError,
    ActionBuildError,
    ActionExecutionAuditError,
    ActionExecutionGuardError,
    ActionExecutionPersistenceError,
    ActionGraphExecutionOutcome,
    ActionGraphExecutionReport,
    ActionQueue,
    ActionQueueRepository,
    ActionQueueStorageError,
    ActionReconciliationOutcome,
    ActionReconciliationReport,
    ApprovedActionGraphExecutor,
    ControlledRollbackExecutor,
    DryRunReport,
    MailboxAction,
    MailboxActionStatus,
    RollbackDryRunReport,
    RollbackExecutionOutcome,
    RollbackExecutionReport,
    RollbackPlanError,
    RollbackReconciliationOutcome,
    RollbackReconciliationReport,
    UncertainActionReconciler,
    UncertainRollbackReconciler,
    audit_event_for_rollback_dry_run,
    audit_events_for_action,
    audit_events_for_dry_run,
    build_dry_run,
    build_review_actions,
    build_rollback_dry_run,
)
from inbox_agent.evaluation import (
    EvaluationReport,
    ExpectedResultsLoadError,
    evaluate_analysis,
    load_expected_results,
)
from inbox_agent.graph import (
    GraphAuthenticationError,
    GraphCategoryWriteClient,
    GraphInboxSynchronizer,
    GraphMailClient,
    GraphRequestError,
    GraphSettingsError,
    GraphSyncReport,
    GraphSyncStorageError,
    GraphTokenProvider,
    GraphWriteDisabledError,
    load_graph_settings,
    load_graph_write_settings,
)
from inbox_agent.llm import (
    LLMFusionPolicyError,
    LLMProviderConfigurationError,
    LLMRouter,
    LLMRoutingPolicyError,
    LLMValidationReport,
    OpenAICompatibleProvider,
    validate_llm_classifications,
)
from inbox_agent.loader import DatasetLoadError, load_dataset
from inbox_agent.models import TriageResult
from inbox_agent.normalizer import normalize_message
from inbox_agent.pipeline import AnalysisReport, OfflinePipeline, analyze_file
from inbox_agent.rule_engine import RulePolicyError
from inbox_agent.service import (
    ServiceAlreadyRunningError,
    ServiceConfigurationError,
    ServiceRunner,
    ServiceRunOutcome,
    ServiceRunResult,
    ServiceStatusReport,
    inspect_service,
    load_service_settings,
)
from inbox_agent.storage import (
    Database,
    MessageRepository,
    UpsertOutcome,
    WorkflowRunRepository,
    current_revision,
    head_revision,
    storage_counts,
    upgrade_database,
)
from inbox_agent.web import WebSettings
from inbox_agent.web.server import run_web_server
from inbox_agent.workflow import (
    WorkflowExecutionError,
    WorkflowReport,
    WorkflowRuntimeSettings,
    WorkflowStatus,
    execute_workflow,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET_PATH = PROJECT_ROOT / "data" / "samples" / "sample_emails.json"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "config" / "rules.yaml"
DEFAULT_EXPECTED_PATH = PROJECT_ROOT / "data" / "eval" / "expected_results.json"
DEFAULT_LLM_ROUTING_PATH = PROJECT_ROOT / "config" / "llm_routing.yaml"
DEFAULT_LLM_FUSION_PATH = PROJECT_ROOT / "config" / "llm_fusion.yaml"
DEFAULT_GRAPH_PATH = PROJECT_ROOT / "config" / "graph.local.yaml"
DEFAULT_GRAPH_WRITE_PATH = PROJECT_ROOT / "config" / "graph_write.local.yaml"
DEFAULT_ACTION_QUEUE_PATH = PROJECT_ROOT / "data" / "private" / "action_queue.json"
DEFAULT_ACTION_AUDIT_PATH = PROJECT_ROOT / "data" / "private" / "audit" / "actions.jsonl"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "private" / "inbox_pilot.sqlite3"
DEFAULT_SERVICE_PATH = PROJECT_ROOT / "config" / "service.local.yaml"


class OutputFormat(StrEnum):
    """Supported CLI output representations."""

    TABLE = "table"
    JSON = "json"


class WebLogLevel(StrEnum):
    """Supported Uvicorn log levels for the managed local server."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


app = typer.Typer(
    name="inbox-agent",
    help="Analyze email priority with explainable rules and an optional LLM.",
    invoke_without_command=True,
)
outlook_app = typer.Typer(
    help="Read-only Outlook sync plus isolated authorization for future approved writes."
)
actions_app = typer.Typer(
    help="Review local actions and run explicitly gated single-action Graph workflows."
)
database_app = typer.Typer(help="Initialize and inspect the private local SQLite database.")
workflow_app = typer.Typer(
    help="Run durable incremental analysis through the human-review boundary."
)
service_app = typer.Typer(
    help="Run one single-instance local scheduler; Ctrl+C stops it gracefully."
)
web_app = typer.Typer(help="Run the loopback-only API and console with managed graceful shutdown.")
app.add_typer(outlook_app, name="outlook")
app.add_typer(actions_app, name="actions")
app.add_typer(database_app, name="db")
app.add_typer(workflow_app, name="workflow")
app.add_typer(service_app, name="service")
app.add_typer(web_app, name="web")


def _console() -> Console:
    """Create a console at invocation time so test runners can capture output."""

    return Console(highlight=False)


def _database_status_payload(database_path: Path) -> dict[str, object]:
    """Inspect a migrated database without exposing any stored message content."""

    resolved = database_path.resolve()
    latest_revision = head_revision(resolved)
    if not resolved.is_file():
        return {
            "database_path": str(resolved),
            "initialized": False,
            "revision": None,
            "head_revision": latest_revision,
            "needs_upgrade": False,
            "counts": {
                "messages": 0,
                "analyses": 0,
                "actions": 0,
                "sync_cursors": 0,
                "workflow_runs": 0,
            },
        }

    database = Database(resolved)
    try:
        revision = current_revision(database.engine)
        counts = storage_counts(database) if revision is not None else None
    finally:
        database.dispose()
    return {
        "database_path": str(resolved),
        "initialized": revision is not None,
        "revision": revision,
        "head_revision": latest_revision,
        "needs_upgrade": revision is not None and revision != latest_revision,
        "counts": (
            {
                "messages": counts.messages,
                "analyses": counts.analyses,
                "actions": counts.actions,
                "sync_cursors": counts.sync_cursors,
                "workflow_runs": counts.workflow_runs,
            }
            if counts is not None
            else {
                "messages": 0,
                "analyses": 0,
                "actions": 0,
                "sync_cursors": 0,
                "workflow_runs": 0,
            }
        ),
    }


def _render_database_status(payload: dict[str, object], output_format: OutputFormat) -> None:
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    counts = payload["counts"]
    assert isinstance(counts, dict)
    table = Table(title="InboxPilot Database")
    table.add_column("字段")
    table.add_column("结果")
    table.add_row("路径", str(payload["database_path"]))
    table.add_row("已初始化", "yes" if payload["initialized"] else "no")
    table.add_row("Revision", str(payload["revision"] or "-"))
    table.add_row("最新 Revision", str(payload["head_revision"] or "-"))
    table.add_row("需要升级", "yes" if payload["needs_upgrade"] else "no")
    table.add_row("邮件", str(counts["messages"]))
    table.add_row("分析结果", str(counts["analyses"]))
    table.add_row("人工动作", str(counts["actions"]))
    table.add_row("同步游标", str(counts["sync_cursors"]))
    table.add_row("工作流运行", str(counts["workflow_runs"]))
    _console().print(table)


def _render_database_import(payload: dict[str, object], output_format: OutputFormat) -> None:
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    table = Table(title="InboxPilot JSON Import")
    table.add_column("字段")
    table.add_column("结果", justify="right")
    table.add_row("数据集", str(payload["dataset_path"]))
    table.add_row("数据库", str(payload["database_path"]))
    table.add_row("新增", str(payload["created"]))
    table.add_row("更新", str(payload["updated"]))
    table.add_row("未变化", str(payload["unchanged"]))
    table.add_row("标准化", str(payload["normalized"]))
    _console().print(table)


def _render_workflow_report(report: WorkflowReport, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return

    table = Table(title="InboxPilot Workflow")
    table.add_column("指标")
    table.add_column("结果", justify="right")
    table.add_row("Run ID", report.run_id)
    table.add_row("状态", report.status.value)
    table.add_row("邮件总数", str(report.total_messages))
    table.add_row("新增/更新", f"{report.imported_created}/{report.imported_updated}")
    table.add_row("无需分析", str(report.skipped_current))
    table.add_row("本次分析", str(report.analyzed_messages))
    table.add_row("分析失败", str(len(report.analysis_failures)))
    table.add_row("LLM 失败", str(len(report.llm_failures)))
    table.add_row("新增动作", str(report.actions_added))
    table.add_row("Graph 写请求", str(report.graph_write_request_count))
    _console().print(table)
    _console().print(f"数据库：[bold]{report.database_path}[/bold]")


def _workflow_status_payload(database_path: Path) -> dict[str, object]:
    resolved = database_path.resolve()
    latest_revision = head_revision(resolved)
    if not resolved.is_file():
        return {
            "database_path": str(resolved),
            "initialized": False,
            "revision": None,
            "head_revision": latest_revision,
            "needs_upgrade": False,
            "latest_run": None,
        }
    database = Database(resolved)
    try:
        revision = current_revision(database.engine)
        latest = (
            WorkflowRunRepository(database).latest()
            if revision is not None and revision == latest_revision
            else None
        )
    finally:
        database.dispose()
    latest_payload = (
        {
            "run_id": latest.run_id,
            "status": latest.status,
            "current_step": latest.current_step,
            "started_at": latest.started_at,
            "finished_at": latest.finished_at,
            "counters": latest.counters,
            "steps": latest.steps,
            "error_summary": latest.error_summary,
        }
        if latest is not None
        else None
    )
    return {
        "database_path": str(resolved),
        "initialized": revision is not None,
        "revision": revision,
        "head_revision": latest_revision,
        "needs_upgrade": revision is not None and revision != latest_revision,
        "latest_run": latest_payload,
    }


def _render_workflow_status(payload: dict[str, object], output_format: OutputFormat) -> None:
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    latest = payload["latest_run"]
    table = Table(title="InboxPilot Workflow Status")
    table.add_column("字段")
    table.add_column("结果")
    table.add_row("数据库", str(payload["database_path"]))
    table.add_row("已初始化", "yes" if payload["initialized"] else "no")
    table.add_row("Revision", str(payload["revision"] or "-"))
    table.add_row("需要升级", "yes" if payload["needs_upgrade"] else "no")
    if isinstance(latest, dict):
        table.add_row("Run ID", str(latest["run_id"]))
        table.add_row("状态", str(latest["status"]))
        table.add_row("当前步骤", str(latest["current_step"] or "-"))
        table.add_row("开始", str(latest["started_at"]))
        table.add_row("结束", str(latest["finished_at"] or "-"))
    else:
        table.add_row("最近运行", "-")
    _console().print(table)


def _render_service_run(result: ServiceRunResult, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    table = Table(title="InboxPilot Service Run")
    table.add_column("指标")
    table.add_column("结果", justify="right")
    table.add_row("服务", result.service_name)
    table.add_row("结果", result.outcome.value)
    table.add_row(
        "Run ID",
        result.workflow_report.run_id if result.workflow_report is not None else "-",
    )
    table.add_row("连续失败", str(result.consecutive_failures))
    table.add_row("下次等待", f"{result.delay_seconds}s")
    table.add_row(
        "Graph 写请求",
        str(
            result.workflow_report.graph_write_request_count
            if result.workflow_report is not None
            else 0
        ),
    )
    if result.error_message is not None:
        table.add_row("错误", result.error_message)
    _console().print(table)


def _render_service_status(report: ServiceStatusReport, output_format: OutputFormat) -> None:
    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    table = Table(title="InboxPilot Service Status")
    table.add_column("字段")
    table.add_column("结果")
    table.add_row("服务", report.service_name)
    table.add_row("OS 锁活动", "yes" if report.active else "no")
    table.add_row("持久化状态", report.persisted_status.value if report.persisted_status else "-")
    table.add_row("PID", str(report.pid or "-"))
    table.add_row("Revision", report.database_revision or "-")
    table.add_row("需要升级", "yes" if report.needs_upgrade else "no")
    table.add_row("上次运行", report.last_run_at.isoformat() if report.last_run_at else "-")
    table.add_row(
        "上次成功",
        report.last_success_at.isoformat() if report.last_success_at else "-",
    )
    table.add_row("连续失败", str(report.consecutive_failures))
    table.add_row("下次运行", report.next_run_at.isoformat() if report.next_run_at else "-")
    if report.last_error is not None:
        table.add_row("最近错误", report.last_error)
    _console().print(table)


def _deadline_text(report_result: TriageResult) -> str:
    """Format an optional TriageResult deadline for terminal output."""

    deadline = report_result.deadline
    if deadline is None:
        return "-"
    return deadline.strftime("%Y-%m-%d %H:%M %z")


def _render_table(report: AnalysisReport, *, show_reasons: bool) -> None:
    """Render successful results, failures, and run totals as Rich tables."""

    console = _console()
    table = Table(title=f"InboxPilot Analysis · {report.policy_version}")
    table.add_column("优先级", justify="center", no_wrap=True)
    table.add_column("分数", justify="right", no_wrap=True)
    table.add_column("类别", no_wrap=True)
    table.add_column("复核", justify="center", no_wrap=True)
    table.add_column("截止时间", no_wrap=True)
    table.add_column("摘要", overflow="fold")

    priority_styles = {
        "P1": "bold red",
        "P2": "yellow",
        "P3": "cyan",
        "P4": "green",
        "P5": "dim",
    }
    for result in report.results:
        priority = result.priority.value
        table.add_row(
            f"[{priority_styles[priority]}]{priority}[/]",
            str(result.score),
            result.category,
            "是" if result.requires_review else "否",
            _deadline_text(result),
            result.summary,
        )
    console.print(table)
    console.print(
        f"成功 [bold]{report.processed_count}[/bold] · "
        f"待复核 [bold]{report.review_count}[/bold] · "
        f"失败 [bold]{report.failure_count}[/bold]"
    )
    if report.llm_routing_decisions:
        console.print(
            f"LLM 路由 [bold]{report.llm_routed_count}[/bold] · "
            f"跳过 [bold]{report.llm_skipped_count}[/bold] · "
            f"成功 [bold]{report.llm_analysis_count}[/bold] · "
            f"融合 [bold]{report.llm_fused_count}[/bold] · "
            f"失败 [bold]{report.llm_failure_count}[/bold]"
        )

    if show_reasons:
        for result in report.results:
            console.print(
                f"\n[bold]{result.priority.value} {result.score} · {result.message_id}[/bold]"
            )
            for reason in result.reasons:
                matched = f" · {reason.matched_value}" if reason.matched_value else ""
                console.print(
                    f"  {reason.score_change:+d} {reason.code}: {reason.description}{matched}"
                )

    if report.failures:
        failure_table = Table(title="Analysis Failures")
        failure_table.add_column("Message ID")
        failure_table.add_column("Stage")
        failure_table.add_column("Error")
        for failure in report.failures:
            failure_table.add_row(
                failure.message_id,
                failure.stage,
                f"{failure.error_type}: {failure.error_message}",
            )
        console.print(failure_table)

    if report.llm_failures:
        failure_table = Table(title="LLM Analysis Failures")
        failure_table.add_column("Message ID")
        failure_table.add_column("Stage")
        failure_table.add_column("Error")
        for failure in report.llm_failures:
            failure_table.add_row(
                failure.message_id,
                failure.stage,
                f"{failure.error_type}: {failure.error_message}",
            )
        console.print(failure_table)


def _render_json(report: AnalysisReport) -> None:
    """Write machine-readable JSON without terminal styling or extra text."""

    typer.echo(
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )


def _render_evaluation_table(report: EvaluationReport) -> None:
    """Render aggregate evaluation metrics and mismatch details."""

    console = _console()
    table = Table(title=f"InboxPilot Evaluation · {report.policy_version}")
    table.add_column("指标")
    table.add_column("结果", justify="right")
    table.add_row("人工标签", str(report.total_labels))
    table.add_row("成功预测", str(report.evaluated_predictions))
    table.add_row("优先级准确率", f"{report.priority_accuracy:.2%}")
    table.add_row("类别准确率", f"{report.category_accuracy:.2%}")
    table.add_row("复核一致率", f"{report.review_accuracy:.2%}")
    table.add_row("P1 精确率", f"{report.p1_precision:.2%}")
    table.add_row("P1 召回率", f"{report.p1_recall:.2%}")
    table.add_row("Pipeline 失败", str(report.analysis_failure_count))
    table.add_row("总体结果", "PASS" if report.passed else "FAIL")
    console.print(table)

    if report.mismatches:
        mismatch_table = Table(title="Prediction Mismatches")
        mismatch_table.add_column("Message ID")
        mismatch_table.add_column("Field")
        mismatch_table.add_column("Expected")
        mismatch_table.add_column("Actual")
        for mismatch in report.mismatches:
            mismatch_table.add_row(
                mismatch.source_id,
                mismatch.field,
                str(mismatch.expected),
                str(mismatch.actual),
            )
        console.print(mismatch_table)


def _render_evaluation_json(report: EvaluationReport) -> None:
    """Write one machine-readable evaluation report."""

    typer.echo(
        json.dumps(
            report.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
    )


def _render_graph_sync_table(report: GraphSyncReport) -> None:
    """Render one read-only Outlook synchronization summary."""

    table = Table(title="InboxPilot Outlook Read-only Sync")
    table.add_column("指标")
    table.add_column("结果", justify="right")
    table.add_row("同步模式", "增量" if report.started_from_delta else "初始")
    table.add_row("完成", "是" if report.completed else "否")
    table.add_row("Graph 页面", str(report.pages_fetched))
    table.add_row("新增", str(report.created_count))
    table.add_row("更新", str(report.updated_count))
    table.add_row("移除", str(report.removed_count))
    table.add_row("未变化", str(report.unchanged_count))
    table.add_row("本地邮件总数", str(report.total_messages))
    table.add_row("映射失败", str(len(report.failures)))
    _console().print(table)
    _console().print(f"私有数据集：[bold]{report.dataset_path}[/bold]")

    if report.failures:
        failure_table = Table(title="Outlook Mapping Failures")
        failure_table.add_column("Message ID")
        failure_table.add_column("Error")
        for failure in report.failures:
            failure_table.add_row(
                failure.message_id,
                f"{failure.error_type}: {failure.error_message}",
            )
        _console().print(failure_table)


def _render_llm_validation_table(report: LLMValidationReport) -> None:
    """Render real-provider classification quality and usage without email bodies."""

    table = Table(title="InboxPilot Real LLM Validation")
    table.add_column("指标")
    table.add_column("结果", justify="right")
    table.add_row("Provider", report.provider or "-")
    table.add_row("模型", report.model_name or "-")
    table.add_row("Prompt", report.prompt_version or "-")
    table.add_row("成功分析", f"{report.analyzed_count}/{report.total_labels}")
    table.add_row("Provider 失败", str(report.provider_failure_count))
    table.add_row("优先级精确率", f"{report.priority_accuracy:.2%}")
    table.add_row("优先级容差率", f"{report.tolerated_priority_accuracy:.2%}")
    table.add_row("类别准确率", f"{report.category_accuracy:.2%}")
    table.add_row("复核准确率", f"{report.review_accuracy:.2%}")
    table.add_row("完全精确率", f"{report.exact_match_accuracy:.2%}")
    table.add_row("完全容差率", f"{report.tolerated_exact_match_accuracy:.2%}")
    table.add_row("输入 Token", str(report.input_tokens))
    table.add_row("输出 Token", str(report.output_tokens))
    table.add_row("缓存命中 Token", str(report.cached_input_tokens))
    table.add_row("累计耗时", f"{report.total_duration_ms / 1000:.2f}s")
    table.add_row("验收结果", "PASS" if report.passed else "FAIL")
    _console().print(table)

    if report.failures:
        failure_table = Table(title="Real LLM Failures")
        failure_table.add_column("Message ID")
        failure_table.add_column("Stage")
        failure_table.add_column("Error")
        for failure in report.failures:
            failure_table.add_row(
                failure.message_id,
                failure.stage,
                f"{failure.error_type}: {failure.error_message}",
            )
        _console().print(failure_table)

    missing_count = sum(mismatch.field == "missing_analysis" for mismatch in report.mismatches)
    visible_mismatches = tuple(
        mismatch for mismatch in report.mismatches if mismatch.field != "missing_analysis"
    )
    if missing_count:
        _console().print(f"未生成结构化分析：[bold]{missing_count}[/bold] 封")

    if report.tolerances:
        tolerance_table = Table(title="Accepted Priority Variances")
        tolerance_table.add_column("Message ID")
        tolerance_table.add_column("标准优先级")
        tolerance_table.add_column("接受结果")
        for tolerance in report.tolerances:
            tolerance_table.add_row(
                tolerance.message_id,
                tolerance.expected,
                tolerance.actual,
            )
        _console().print(tolerance_table)

    if visible_mismatches:
        mismatch_table = Table(title="Real LLM Mismatches")
        mismatch_table.add_column("Message ID")
        mismatch_table.add_column("字段")
        mismatch_table.add_column("期望")
        mismatch_table.add_column("实际")
        for mismatch in visible_mismatches:
            mismatch_table.add_row(
                mismatch.message_id,
                mismatch.field,
                mismatch.expected,
                mismatch.actual or "-",
            )
        _console().print(mismatch_table)


def _filtered_queue(
    queue: ActionQueue,
    status: MailboxActionStatus | None,
) -> tuple[MailboxAction, ...]:
    if status is None:
        return queue.actions
    return tuple(action for action in queue.actions if action.status is status)


def _render_action_queue(
    queue: ActionQueue,
    queue_path: Path,
    *,
    status: MailboxActionStatus | None,
    output_format: OutputFormat,
) -> None:
    """Render a filtered local review queue without exposing message bodies."""

    actions = _filtered_queue(queue, status)
    if output_format is OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "schema_version": queue.schema_version,
                    "queue_path": str(queue_path),
                    "updated_at": queue.updated_at.isoformat(),
                    "actions": [action.model_dump(mode="json") for action in actions],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    table = Table(title="InboxPilot Human Review Queue")
    table.add_column("Action ID", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("优先级", justify="center")
    table.add_column("类别", no_wrap=True)
    table.add_column("模型来源", no_wrap=True)
    table.add_column("摘要", overflow="fold")
    for action in actions:
        result = action.evidence.triage_result
        table.add_row(
            action.action_id,
            action.status.value,
            result.priority.value,
            result.category,
            result.decision_source.value,
            result.summary,
        )
    _console().print(table)
    _console().print(
        f"显示 [bold]{len(actions)}[/bold] · "
        f"队列总数 [bold]{len(queue.actions)}[/bold] · "
        f"待确认 [bold]{queue.pending_count}[/bold]"
    )
    _console().print(f"本地队列：[bold]{queue_path}[/bold]")


def _render_action(action: MailboxAction, output_format: OutputFormat) -> None:
    """Render one review action and its decision evidence."""

    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(action.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return

    result = action.evidence.triage_result
    llm_priority = (
        action.evidence.llm_analysis.analysis.priority.value
        if action.evidence.llm_analysis is not None
        else "-"
    )
    table = Table(title=f"InboxPilot Action · {action.action_id}")
    table.add_column("字段")
    table.add_column("值", overflow="fold")
    table.add_row("状态", action.status.value)
    table.add_row("邮件 ID", action.message_id)
    table.add_row("动作", action.action_type.value)
    table.add_row("最终优先级", result.priority.value)
    table.add_row("类别", result.category)
    table.add_row("规则优先级", action.evidence.rule_evaluation.suggested_priority.value)
    table.add_row("LLM 优先级", llm_priority)
    table.add_row("决策来源", result.decision_source.value)
    table.add_row("需要复核", "是" if result.requires_review else "否")
    table.add_row("当前分类", ", ".join(action.current_snapshot.categories) or "-")
    table.add_row("建议分类", ", ".join(action.write_plan.managed_categories))
    table.add_row("摘要", result.summary)
    table.add_row("状态变更数", str(len(action.transition_history)))
    _console().print(table)


def _render_dry_run(report: DryRunReport, output_format: OutputFormat) -> None:
    """Render category differences while explicitly reporting zero Graph writes."""

    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return

    table = Table(title="InboxPilot Category Write Dry-run")
    table.add_column("Action ID", no_wrap=True)
    table.add_column("当前分类", overflow="fold")
    table.add_column("新增", overflow="fold")
    table.add_column("移除", overflow="fold")
    table.add_column("最终分类", overflow="fold")
    table.add_column("需写入", justify="center")
    for plan in report.plans:
        table.add_row(
            plan.action_id,
            ", ".join(plan.current_categories) or "-",
            ", ".join(plan.add_categories) or "-",
            ", ".join(plan.remove_categories) or "-",
            ", ".join(plan.final_categories) or "-",
            "是" if plan.would_write else "否",
        )
    _console().print(table)
    _console().print(
        f"队列 [bold]{report.queue_total_count}[/bold] · "
        f"已批准 [bold]{report.eligible_count}[/bold] · "
        f"跳过 [bold]{report.skipped_count}[/bold] · "
        f"需变更 [bold]{report.would_write_count}[/bold] · "
        f"无需变更 [bold]{report.no_change_count}[/bold]"
    )
    _console().print(
        f"Graph 写请求：[bold green]{report.graph_write_request_count}[/bold green]（dry-run）"
    )


def _render_rollback_dry_run(
    report: RollbackDryRunReport,
    output_format: OutputFormat,
) -> None:
    """Render one rollback difference while proving that Graph writes remain zero."""

    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return

    plan = report.plan
    table = Table(title="InboxPilot Controlled Rollback Dry-run")
    table.add_column("字段")
    table.add_column("结果", overflow="fold")
    table.add_row("Action ID", plan.action_id)
    table.add_row("状态", plan.source_status.value)
    table.add_row("回滚原因", plan.reason)
    table.add_row("预期当前分类", ", ".join(plan.expected_current_categories) or "-")
    table.add_row("恢复分类", ", ".join(plan.restore_managed_categories) or "-")
    table.add_row("新增", ", ".join(plan.add_categories) or "-")
    table.add_row("移除", ", ".join(plan.remove_categories) or "-")
    table.add_row("最终分类", ", ".join(plan.final_categories) or "-")
    table.add_row("回滚幂等键", plan.rollback_idempotency_key)
    table.add_row("需要变更", "是" if plan.would_write else "否")
    table.add_row("Graph 写请求", str(report.graph_write_request_count))
    _console().print(table)
    _console().print(
        "[yellow]提示：这是基于动作快照的本地预览。真实回滚前必须重新读取 Outlook "
        "分类并校验最新 changeKey。[/yellow]"
    )


def _render_action_execution(
    report: ActionGraphExecutionReport,
    output_format: OutputFormat,
) -> None:
    """Render one bounded Graph execution result without mailbox content."""

    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return

    table = Table(title="InboxPilot Single-action Graph Execution")
    table.add_column("字段")
    table.add_column("结果", overflow="fold")
    table.add_row("Action ID", report.action_id)
    table.add_row("结果", report.outcome.value)
    table.add_row("最终状态", report.final_status.value)
    table.add_row("执行次数", str(report.attempt_number))
    table.add_row("Graph 读取请求", str(report.graph_read_request_count))
    table.add_row("Graph 写入请求", str(report.graph_write_request_count))
    table.add_row("说明", report.reason or "-")
    _console().print(table)


def _render_action_reconciliation(
    report: ActionReconciliationReport,
    output_format: OutputFormat,
) -> None:
    """Render one read-only uncertain-write reconciliation result."""

    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return

    table = Table(title="InboxPilot Read-only Graph Reconciliation")
    table.add_column("字段")
    table.add_column("结果", overflow="fold")
    table.add_row("Action ID", report.action_id)
    table.add_row("对账结果", report.outcome.value)
    table.add_row("最终状态", report.final_status.value)
    table.add_row("Graph 读取请求", str(report.graph_read_request_count))
    table.add_row("Graph 写入请求", str(report.graph_write_request_count))
    table.add_row("说明", report.reason)
    _console().print(table)


def _render_rollback_execution(
    report: RollbackExecutionReport,
    output_format: OutputFormat,
) -> None:
    """Render a real controlled rollback without exposing mailbox content."""

    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    table = Table(title="InboxPilot Controlled Rollback Execution")
    table.add_column("Field")
    table.add_column("Result", overflow="fold")
    table.add_row("Action ID", report.action_id)
    table.add_row("Outcome", report.outcome.value)
    table.add_row("Final status", report.final_status.value)
    table.add_row("Attempt", str(report.attempt_number))
    table.add_row("Graph reads", str(report.graph_read_request_count))
    table.add_row("Graph writes", str(report.graph_write_request_count))
    table.add_row("Reason", report.reason or "-")
    _console().print(table)


def _render_rollback_reconciliation(
    report: RollbackReconciliationReport,
    output_format: OutputFormat,
) -> None:
    """Render the zero-write reconciliation of an uncertain rollback."""

    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return
    table = Table(title="InboxPilot Rollback Reconciliation")
    table.add_column("Field")
    table.add_column("Result", overflow="fold")
    table.add_row("Action ID", report.action_id)
    table.add_row("Outcome", report.outcome.value)
    table.add_row("Final status", report.final_status.value)
    table.add_row("Graph reads", str(report.graph_read_request_count))
    table.add_row("Graph writes", str(report.graph_write_request_count))
    table.add_row("Reason", report.reason)
    _console().print(table)


def _run(
    dataset_path: Path,
    policy_path: Path,
    output_format: OutputFormat,
    show_reasons: bool,
    llm_config_path: Path | None = None,
    llm_routing_path: Path = DEFAULT_LLM_ROUTING_PATH,
    llm_fusion_path: Path = DEFAULT_LLM_FUSION_PATH,
) -> None:
    """Execute one CLI analysis with consistent error and exit handling."""

    try:
        llm_provider = (
            OpenAICompatibleProvider.from_yaml(llm_config_path)
            if llm_config_path is not None
            else None
        )
        report = analyze_file(
            dataset_path,
            policy_path,
            llm_provider=llm_provider,
            llm_routing_path=llm_routing_path if llm_provider is not None else None,
            llm_fusion_path=llm_fusion_path if llm_provider is not None else None,
        )
    except (
        DatasetLoadError,
        RulePolicyError,
        LLMProviderConfigurationError,
        LLMRoutingPolicyError,
        LLMFusionPolicyError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    if output_format is OutputFormat.JSON:
        _render_json(report)
    else:
        _render_table(report, show_reasons=show_reasons)

    if report.failure_count or report.llm_failure_count:
        raise typer.Exit(code=2)


@app.callback()
def main(context: typer.Context) -> None:
    """Display command help when InboxPilot is called without a subcommand."""

    if context.invoked_subcommand is None:
        typer.echo(context.get_help())


@database_app.command("init")
def database_init(
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            help="Private SQLite path. The default lives under Git-ignored data/private/.",
        ),
    ] = DEFAULT_DATABASE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Create or upgrade the private SQLite database to the latest schema."""

    try:
        upgrade_database(database_path)
        payload = _database_status_payload(database_path)
    except (AlembicCommandError, OSError, SQLAlchemyError) as error:
        typer.echo(f"Error: database initialization failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    _render_database_status(payload, output_format)


@database_app.command("status")
def database_status(
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            help="Private SQLite path. This command never creates a missing database.",
        ),
    ] = DEFAULT_DATABASE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Show schema revision and record counts without reading private payloads."""

    try:
        payload = _database_status_payload(database_path)
    except (OSError, SQLAlchemyError) as error:
        typer.echo(f"Error: database inspection failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    _render_database_status(payload, output_format)


@database_app.command("import-json")
def database_import_json(
    dataset_path: Annotated[
        Path,
        typer.Argument(help="Existing InboxPilot JSON dataset to import."),
    ],
    database_path: Annotated[
        Path,
        typer.Option(
            "--database",
            help="Private SQLite path. The schema is upgraded before import.",
        ),
    ] = DEFAULT_DATABASE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Idempotently import and normalize messages from the existing JSON format."""

    database: Database | None = None
    try:
        dataset = load_dataset(dataset_path)
        upgrade_database(database_path)
        database = Database(database_path)
        repository = MessageRepository(database)
        created = 0
        updated = 0
        unchanged = 0
        normalized = 0
        for message in dataset.messages:
            result = repository.upsert(message)
            if result.outcome is UpsertOutcome.CREATED:
                created += 1
            elif result.outcome is UpsertOutcome.UPDATED:
                updated += 1
            else:
                unchanged += 1
            repository.save_normalized(normalize_message(message))
            normalized += 1
        payload: dict[str, object] = {
            "dataset_path": str(dataset_path.resolve()),
            "database_path": str(database_path.resolve()),
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "normalized": normalized,
        }
    except (AlembicCommandError, DatasetLoadError, OSError, SQLAlchemyError) as error:
        typer.echo(f"Error: JSON import failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    finally:
        if database is not None:
            database.dispose()
    _render_database_import(payload, output_format)


@workflow_app.command("run")
def workflow_run(
    dataset_path: Annotated[
        Path,
        typer.Option("--dataset", "-d", help="JSON dataset used when Outlook sync is disabled."),
    ] = DEFAULT_DATASET_PATH,
    database_path: Annotated[
        Path,
        typer.Option("--database", help="Private SQLite workflow database."),
    ] = DEFAULT_DATABASE_PATH,
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Private human-review action queue."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    audit_path: Annotated[
        Path,
        typer.Option("--audit-log", help="Private append-only action audit log."),
    ] = DEFAULT_ACTION_AUDIT_PATH,
    policy_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Deterministic YAML rule policy."),
    ] = DEFAULT_POLICY_PATH,
    llm_config_path: Annotated[
        Path | None,
        typer.Option("--llm-config", help="Optional local OpenAI/DeepSeek provider YAML."),
    ] = None,
    llm_routing_path: Annotated[
        Path,
        typer.Option("--llm-routing-config", help="LLM routing policy."),
    ] = DEFAULT_LLM_ROUTING_PATH,
    llm_fusion_path: Annotated[
        Path,
        typer.Option("--llm-fusion-config", help="Rule/LLM fusion policy."),
    ] = DEFAULT_LLM_FUSION_PATH,
    sync_outlook: Annotated[
        bool,
        typer.Option(
            "--sync-outlook",
            help="Run one read-only Outlook delta sync before analysis.",
        ),
    ] = False,
    graph_config_path: Annotated[
        Path,
        typer.Option("--graph-config", help="Private read-only Graph configuration."),
    ] = DEFAULT_GRAPH_PATH,
    force: Annotated[
        bool,
        typer.Option("--force", help="Reanalyze even when content and configuration are current."),
    ] = False,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Synchronize optionally, analyze incrementally, and build review-only actions."""

    try:
        report = execute_workflow(
            WorkflowRuntimeSettings(
                project_root=PROJECT_ROOT,
                dataset_path=dataset_path,
                database_path=database_path,
                action_queue_path=queue_path,
                audit_log_path=audit_path,
                policy_path=policy_path,
                llm_config_path=llm_config_path,
                llm_routing_path=llm_routing_path,
                llm_fusion_path=llm_fusion_path,
                sync_outlook=sync_outlook,
                graph_config_path=graph_config_path,
            ),
            force=force,
        )
    except (
        AlembicCommandError,
        GraphSettingsError,
        LLMProviderConfigurationError,
        LLMRoutingPolicyError,
        LLMFusionPolicyError,
        OSError,
        RulePolicyError,
        SQLAlchemyError,
        WorkflowExecutionError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    _render_workflow_report(report, output_format)
    if report.status is WorkflowStatus.COMPLETED_WITH_FAILURES:
        raise typer.Exit(code=2)


@workflow_app.command("status")
def workflow_status(
    database_path: Annotated[
        Path,
        typer.Option("--database", help="Private SQLite workflow database."),
    ] = DEFAULT_DATABASE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Show the latest durable workflow run without reading email payloads."""

    try:
        payload = _workflow_status_payload(database_path)
    except (OSError, SQLAlchemyError) as error:
        typer.echo(f"Error: workflow status failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    _render_workflow_status(payload, output_format)


def _service_runner_from_config(
    config_path: Path,
) -> tuple[ServiceRunner, Database]:
    settings = load_service_settings(config_path)
    runtime = settings.workflow.runtime_settings(PROJECT_ROOT)
    upgrade_database(runtime.database_path)
    database = Database(runtime.database_path)
    runner = ServiceRunner(
        settings=settings,
        database=database,
        lock_path=settings.resolved_lock_path(PROJECT_ROOT),
        execute_workflow=lambda: execute_workflow(runtime),
    )
    return runner, database


@service_app.command("run-once")
def service_run_once(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Private local scheduler YAML."),
    ] = DEFAULT_SERVICE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Run one locked workflow attempt and return without scheduling another."""

    database: Database | None = None
    try:
        runner, database = _service_runner_from_config(config_path)
        result = runner.run_once()
    except (
        AlembicCommandError,
        OSError,
        SQLAlchemyError,
        ServiceAlreadyRunningError,
        ServiceConfigurationError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    finally:
        if database is not None:
            database.dispose()
    _render_service_run(result, output_format)
    if result.outcome is ServiceRunOutcome.FAILED:
        raise typer.Exit(code=1)
    if result.outcome is ServiceRunOutcome.COMPLETED_WITH_FAILURES:
        raise typer.Exit(code=2)


@service_app.command("start")
def service_start(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Private local scheduler YAML."),
    ] = DEFAULT_SERVICE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format for each completed attempt."),
    ] = OutputFormat.TABLE,
    max_runs: Annotated[
        int | None,
        typer.Option(
            "--max-runs",
            min=1,
            help="Stop after N attempts; useful for controlled acceptance tests.",
        ),
    ] = None,
) -> None:
    """Run a foreground scheduler until Ctrl+C, preserving durable status."""

    database: Database | None = None
    try:
        runner, database = _service_runner_from_config(config_path)
        typer.echo(
            f"InboxPilot service started; Ctrl+C to stop; config={config_path}",
            err=True,
        )
        results = runner.serve(
            on_result=lambda result: _render_service_run(result, output_format),
            max_runs=max_runs,
        )
    except KeyboardInterrupt:
        typer.echo("InboxPilot service stopped gracefully.", err=True)
        return
    except (
        AlembicCommandError,
        OSError,
        SQLAlchemyError,
        ServiceAlreadyRunningError,
        ServiceConfigurationError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    finally:
        if database is not None:
            database.dispose()
    if any(result.outcome is ServiceRunOutcome.FAILED for result in results):
        raise typer.Exit(code=1)
    if any(result.outcome is ServiceRunOutcome.COMPLETED_WITH_FAILURES for result in results):
        raise typer.Exit(code=2)


@service_app.command("status")
def service_status(
    config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Private local scheduler YAML."),
    ] = DEFAULT_SERVICE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Inspect OS-lock liveness and persisted state without starting the service."""

    try:
        settings = load_service_settings(config_path)
        report = inspect_service(
            settings,
            config_path=config_path,
            project_root=PROJECT_ROOT,
        )
    except (OSError, SQLAlchemyError, ServiceConfigurationError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    _render_service_status(report, output_format)


@web_app.command("start")
def web_start(
    port: Annotated[
        int,
        typer.Option("--port", min=1, max=65535, help="Loopback TCP port."),
    ] = 8765,
    log_level: Annotated[
        WebLogLevel,
        typer.Option("--log-level", help="Uvicorn log level."),
    ] = WebLogLevel.INFO,
) -> None:
    """Start the managed local Web process on 127.0.0.1."""

    typer.echo(
        f"InboxPilot Web started at http://127.0.0.1:{port}/console; "
        "use the page controls for background mode or full exit.",
        err=True,
    )
    try:
        run_web_server(
            WebSettings.from_environment(),
            port=port,
            log_level=log_level.value,
        )
    except KeyboardInterrupt:
        typer.echo("InboxPilot Web stopped gracefully.", err=True)


@app.command()
def demo(
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
    show_reasons: Annotated[
        bool,
        typer.Option("--show-reasons", help="Show every score contribution in table mode."),
    ] = False,
) -> None:
    """Analyze the bundled anonymous sample dataset."""

    _run(
        DEFAULT_DATASET_PATH,
        DEFAULT_POLICY_PATH,
        output_format,
        show_reasons,
    )


@app.command()
def analyze(
    dataset_path: Annotated[
        Path,
        typer.Argument(help="Path to an InboxPilot MessageDataset JSON file."),
    ],
    policy_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to a YAML rule policy."),
    ] = DEFAULT_POLICY_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
    show_reasons: Annotated[
        bool,
        typer.Option("--show-reasons", help="Show every score contribution in table mode."),
    ] = False,
    llm_config_path: Annotated[
        Path | None,
        typer.Option(
            "--llm-config",
            help="Path to a local OpenAI/DeepSeek provider YAML file.",
        ),
    ] = None,
    llm_routing_path: Annotated[
        Path,
        typer.Option("--llm-routing-config", help="Path to the LLM routing YAML policy."),
    ] = DEFAULT_LLM_ROUTING_PATH,
    llm_fusion_path: Annotated[
        Path,
        typer.Option("--llm-fusion-config", help="Path to the LLM fusion YAML policy."),
    ] = DEFAULT_LLM_FUSION_PATH,
) -> None:
    """Analyze email JSON with rules and an optional real LLM provider."""

    _run(
        dataset_path,
        policy_path,
        output_format,
        show_reasons,
        llm_config_path,
        llm_routing_path,
        llm_fusion_path,
    )


@actions_app.command("build")
def actions_build(
    dataset_path: Annotated[
        Path,
        typer.Option("--dataset", "-d", help="Path to a local MessageDataset JSON file."),
    ] = DEFAULT_DATASET_PATH,
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    audit_path: Annotated[
        Path,
        typer.Option("--audit-log", help="Path to the private append-only audit JSONL file."),
    ] = DEFAULT_ACTION_AUDIT_PATH,
    policy_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to the deterministic rule policy."),
    ] = DEFAULT_POLICY_PATH,
    llm_config_path: Annotated[
        Path | None,
        typer.Option("--llm-config", help="Optional local OpenAI/DeepSeek provider YAML."),
    ] = None,
    llm_routing_path: Annotated[
        Path,
        typer.Option("--llm-routing-config", help="Path to the LLM routing policy."),
    ] = DEFAULT_LLM_ROUTING_PATH,
    llm_fusion_path: Annotated[
        Path,
        typer.Option("--llm-fusion-config", help="Path to the LLM fusion policy."),
    ] = DEFAULT_LLM_FUSION_PATH,
) -> None:
    """Analyze a local dataset and add pending category actions to the private queue."""

    try:
        dataset = load_dataset(dataset_path)
        llm_provider = (
            OpenAICompatibleProvider.from_yaml(llm_config_path)
            if llm_config_path is not None
            else None
        )
        pipeline = OfflinePipeline.from_yaml(
            policy_path,
            llm_provider=llm_provider,
            llm_routing_path=llm_routing_path if llm_provider is not None else None,
            llm_fusion_path=llm_fusion_path if llm_provider is not None else None,
        )
        analysis = pipeline.analyze_dataset(dataset)
        actions = build_review_actions(dataset, analysis)
        repository = ActionQueueRepository(queue_path)
        update = repository.enqueue(actions)
        queue = repository.load()
        audit_events = tuple(
            event for action in queue.actions for event in audit_events_for_action(action)
        )
        audit_update = ActionAuditLog(audit_path).append_unique(audit_events)
    except (
        DatasetLoadError,
        RulePolicyError,
        LLMProviderConfigurationError,
        LLMRoutingPolicyError,
        LLMFusionPolicyError,
        ActionBuildError,
        ActionQueueStorageError,
        ActionAuditStorageError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    table = Table(title="InboxPilot Action Queue Build")
    table.add_column("指标")
    table.add_column("结果", justify="right")
    table.add_row("生成动作", str(update.generated_count))
    table.add_row("新增队列", str(update.added_count))
    table.add_row("重复跳过", str(update.skipped_count))
    table.add_row("队列总数", str(update.total_count))
    table.add_row("待确认", str(update.pending_count))
    table.add_row("新增审计", str(audit_update.appended_count))
    table.add_row("审计去重", str(audit_update.skipped_count))
    _console().print(table)
    _console().print(f"本地队列：[bold]{update.queue_path}[/bold]")
    _console().print(f"审计日志：[bold]{audit_update.log_path}[/bold]")

    if analysis.failure_count or analysis.llm_failure_count:
        raise typer.Exit(code=2)


@actions_app.command("list")
def actions_list(
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    status: Annotated[
        MailboxActionStatus | None,
        typer.Option("--status", help="Only show actions in this state."),
    ] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """List locally queued actions without changing them."""

    try:
        queue = ActionQueueRepository(queue_path).load()
    except ActionQueueStorageError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    _render_action_queue(queue, queue_path, status=status, output_format=output_format)


@actions_app.command("show")
def actions_show(
    action_id: Annotated[str, typer.Argument(help="Action ID to inspect.")],
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Show one action, its planned categories, and decision evidence."""

    try:
        queue = ActionQueueRepository(queue_path).load()
        action = queue.find(action_id)
        if action is None:
            raise ActionQueueStorageError(f"Action does not exist: {action_id}")
    except ActionQueueStorageError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    _render_action(action, output_format)


def _review_action(
    action_id: str,
    queue_path: Path,
    audit_path: Path,
    to_status: MailboxActionStatus,
    note: str | None,
) -> None:
    try:
        action = ActionQueueRepository(queue_path).transition(
            action_id,
            to_status,
            actor=ActionActor.USER,
            note=note,
        )
        audit_update = ActionAuditLog(audit_path).append_unique(audit_events_for_action(action))
    except (ActionQueueStorageError, ActionAuditStorageError, ValidationError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Action {action.action_id} is now {action.status.value}.")
    typer.echo(f"Audit appended {audit_update.appended_count}; log: {audit_update.log_path}")


@actions_app.command("approve")
def actions_approve(
    action_id: Annotated[str, typer.Argument(help="Pending action ID to approve.")],
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    audit_path: Annotated[
        Path,
        typer.Option("--audit-log", help="Path to the private append-only audit JSONL file."),
    ] = DEFAULT_ACTION_AUDIT_PATH,
    note: Annotated[
        str | None,
        typer.Option("--note", help="Optional human review note."),
    ] = None,
) -> None:
    """Explicitly approve one pending action; no Graph write is performed."""

    _review_action(action_id, queue_path, audit_path, MailboxActionStatus.APPROVED, note)


@actions_app.command("reject")
def actions_reject(
    action_id: Annotated[str, typer.Argument(help="Pending or failed action ID to reject.")],
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    audit_path: Annotated[
        Path,
        typer.Option("--audit-log", help="Path to the private append-only audit JSONL file."),
    ] = DEFAULT_ACTION_AUDIT_PATH,
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Optional rejection reason."),
    ] = None,
) -> None:
    """Reject one action; no Graph write is performed."""

    _review_action(action_id, queue_path, audit_path, MailboxActionStatus.REJECTED, reason)


@actions_app.command("apply")
def actions_apply(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Preview approved category actions without sending Graph requests.",
        ),
    ] = False,
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    audit_path: Annotated[
        Path,
        typer.Option("--audit-log", help="Path to the private append-only audit JSONL file."),
    ] = DEFAULT_ACTION_AUDIT_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Preview approved actions; real execution is deliberately unavailable."""

    if not dry_run:
        typer.echo(
            "Error: Stage 3 currently supports only actions apply --dry-run; "
            "no Graph write executor is enabled.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        queue = ActionQueueRepository(queue_path).load()
        report = build_dry_run(queue, queue_path)
        history_events = tuple(
            event for action in queue.actions for event in audit_events_for_action(action)
        )
        dry_run_events = audit_events_for_dry_run(queue.actions, report)
        audit_update = ActionAuditLog(audit_path).append_unique((*history_events, *dry_run_events))
    except (ActionQueueStorageError, ActionAuditStorageError, ValidationError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    _render_dry_run(report, output_format)
    if output_format is OutputFormat.TABLE:
        _console().print(
            f"审计新增 [bold]{audit_update.appended_count}[/bold] · "
            f"去重 [bold]{audit_update.skipped_count}[/bold] · "
            f"日志 [bold]{audit_update.log_path}[/bold]"
        )


@actions_app.command("rollback")
def actions_rollback(
    action_id: Annotated[str, typer.Argument(help="Succeeded action ID to preview rollback.")],
    reason: Annotated[
        str,
        typer.Option("--reason", help="Required human reason for the rollback preview."),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Preview rollback categories without sending Graph requests.",
        ),
    ] = False,
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    audit_path: Annotated[
        Path,
        typer.Option("--audit-log", help="Path to the private append-only audit JSONL file."),
    ] = DEFAULT_ACTION_AUDIT_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Preview one controlled rollback; real Graph execution is unavailable."""

    if not dry_run:
        typer.echo(
            "Error: Stage 3 currently supports only actions rollback --dry-run; "
            "no Graph rollback executor is enabled.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        queue = ActionQueueRepository(queue_path).load()
        report = build_rollback_dry_run(
            queue,
            action_id,
            queue_path,
            reason=reason,
        )
        action = queue.find(action_id)
        if action is None:
            raise RollbackPlanError(f"Action does not exist: {action_id}")
        history_events = audit_events_for_action(action)
        rollback_event = audit_event_for_rollback_dry_run(action, report)
        audit_update = ActionAuditLog(audit_path).append_unique((*history_events, rollback_event))
    except (
        ActionQueueStorageError,
        ActionAuditStorageError,
        RollbackPlanError,
        ValidationError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    _render_rollback_dry_run(report, output_format)
    if output_format is OutputFormat.TABLE:
        _console().print(
            f"审计新增 [bold]{audit_update.appended_count}[/bold] · "
            f"去重 [bold]{audit_update.skipped_count}[/bold] · "
            f"日志 [bold]{audit_update.log_path}[/bold]"
        )


@actions_app.command("rollback-execute")
def actions_rollback_execute(
    action_id: Annotated[str, typer.Argument(help="One succeeded action ID to roll back.")],
    reason: Annotated[str, typer.Option("--reason", help="Required human rollback reason.")],
    rollback_idempotency_key: Annotated[
        str,
        typer.Option(
            "--rollback-idempotency-key",
            help="Exact key produced by actions rollback --dry-run.",
        ),
    ],
    confirm_action: Annotated[
        str | None,
        typer.Option("--confirm-action", help="Repeat the exact action ID to confirm rollback."),
    ] = None,
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    audit_path: Annotated[
        Path,
        typer.Option("--audit-log", help="Path to the private append-only audit JSONL file."),
    ] = DEFAULT_ACTION_AUDIT_PATH,
    graph_config_path: Annotated[
        Path,
        typer.Option("--graph-config", help="Private Graph write-authorization YAML file."),
    ] = DEFAULT_GRAPH_WRITE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Restore one action's original InboxPilot categories after confirmation."""

    if confirm_action != action_id:
        typer.echo(
            "Error: rollback confirmation denied; pass --confirm-action with the exact "
            f"action ID ({action_id}). No Graph request was sent.",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        settings = load_graph_write_settings(graph_config_path)
        settings.require_enabled()
        provider = GraphTokenProvider.from_settings(settings, PROJECT_ROOT)
        token = provider.acquire_silent()
        repository = ActionQueueRepository(queue_path)
        audit_log = ActionAuditLog(audit_path)
        with httpx.Client() as http_client:
            report = ControlledRollbackExecutor(
                repository,
                GraphCategoryWriteClient(settings, http_client),
                audit_log,
            ).execute(action_id, rollback_idempotency_key, reason, token)
    except (
        GraphSettingsError,
        GraphWriteDisabledError,
        GraphAuthenticationError,
        GraphRequestError,
        ActionQueueStorageError,
        ActionAuditStorageError,
        ActionExecutionGuardError,
        ActionExecutionPersistenceError,
        ActionExecutionAuditError,
        ValidationError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    _render_rollback_execution(report, output_format)
    if report.outcome in {
        RollbackExecutionOutcome.CONFLICT,
        RollbackExecutionOutcome.FAILED,
        RollbackExecutionOutcome.OUTCOME_UNKNOWN,
    }:
        raise typer.Exit(code=2)


@actions_app.command("rollback-reconcile")
def actions_rollback_reconcile(
    action_id: Annotated[str, typer.Argument(help="One uncertain rollback action ID.")],
    rollback_idempotency_key: Annotated[
        str,
        typer.Option("--rollback-idempotency-key", help="Exact rollback idempotency key."),
    ],
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    audit_path: Annotated[
        Path,
        typer.Option("--audit-log", help="Path to the private append-only audit JSONL file."),
    ] = DEFAULT_ACTION_AUDIT_PATH,
    graph_config_path: Annotated[
        Path,
        typer.Option("--graph-config", help="Private Graph write-authorization YAML file."),
    ] = DEFAULT_GRAPH_WRITE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Resolve an uncertain rollback with one GET and zero PATCH requests."""

    try:
        settings = load_graph_write_settings(graph_config_path)
        settings.require_enabled()
        provider = GraphTokenProvider.from_settings(settings, PROJECT_ROOT)
        token = provider.acquire_silent()
        repository = ActionQueueRepository(queue_path)
        audit_log = ActionAuditLog(audit_path)
        with httpx.Client() as http_client:
            report = UncertainRollbackReconciler(
                repository,
                GraphCategoryWriteClient(settings, http_client),
                audit_log,
            ).reconcile(action_id, rollback_idempotency_key, token)
    except (
        GraphSettingsError,
        GraphWriteDisabledError,
        GraphAuthenticationError,
        GraphRequestError,
        ActionQueueStorageError,
        ActionAuditStorageError,
        ActionExecutionGuardError,
        ActionExecutionPersistenceError,
        ActionExecutionAuditError,
        ValidationError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    _render_rollback_reconciliation(report, output_format)
    if report.outcome in {
        RollbackReconciliationOutcome.CONFLICT,
        RollbackReconciliationOutcome.READ_FAILED,
    }:
        raise typer.Exit(code=2)


@actions_app.command("execute")
def actions_execute(
    action_id: Annotated[
        str,
        typer.Argument(help="One approved action ID to execute."),
    ],
    idempotency_key: Annotated[
        str,
        typer.Option(
            "--idempotency-key",
            help="Exact idempotency key recorded on the approved action.",
        ),
    ],
    confirm_action: Annotated[
        str | None,
        typer.Option(
            "--confirm-action",
            help="Repeat the exact action ID to unlock this single Graph write workflow.",
        ),
    ] = None,
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    audit_path: Annotated[
        Path,
        typer.Option("--audit-log", help="Path to the private append-only audit JSONL file."),
    ] = DEFAULT_ACTION_AUDIT_PATH,
    graph_config_path: Annotated[
        Path,
        typer.Option(
            "--graph-config",
            help="Path to the private Graph write-authorization YAML file.",
        ),
    ] = DEFAULT_GRAPH_WRITE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Execute exactly one approved category action after an explicit confirmation gate."""

    if confirm_action != action_id:
        typer.echo(
            "Error: write confirmation denied; pass --confirm-action with the exact "
            f"action ID ({action_id}). No Graph request was sent.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        settings = load_graph_write_settings(graph_config_path)
        settings.require_enabled()
        provider = GraphTokenProvider.from_settings(settings, PROJECT_ROOT)
        token = provider.acquire_silent()
        repository = ActionQueueRepository(queue_path)
        audit_log = ActionAuditLog(audit_path)
        with httpx.Client() as http_client:
            graph_client = GraphCategoryWriteClient(settings, http_client)
            report = ApprovedActionGraphExecutor(
                repository,
                graph_client,
                audit_log,
            ).execute(action_id, idempotency_key, token)
    except (
        GraphSettingsError,
        GraphWriteDisabledError,
        GraphAuthenticationError,
        GraphRequestError,
        ActionQueueStorageError,
        ActionAuditStorageError,
        ActionExecutionGuardError,
        ActionExecutionPersistenceError,
        ActionExecutionAuditError,
        ValidationError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    _render_action_execution(report, output_format)
    if report.outcome in {
        ActionGraphExecutionOutcome.CONFLICT,
        ActionGraphExecutionOutcome.FAILED,
        ActionGraphExecutionOutcome.OUTCOME_UNKNOWN,
    }:
        raise typer.Exit(code=2)


@actions_app.command("reconcile")
def actions_reconcile(
    action_id: Annotated[
        str,
        typer.Argument(help="One uncertain action ID to reconcile with a read-only GET."),
    ],
    idempotency_key: Annotated[
        str,
        typer.Option(
            "--idempotency-key",
            help="Exact idempotency key recorded on the uncertain action.",
        ),
    ],
    queue_path: Annotated[
        Path,
        typer.Option("--queue", help="Path to the private action queue JSON file."),
    ] = DEFAULT_ACTION_QUEUE_PATH,
    audit_path: Annotated[
        Path,
        typer.Option("--audit-log", help="Path to the private append-only audit JSONL file."),
    ] = DEFAULT_ACTION_AUDIT_PATH,
    graph_config_path: Annotated[
        Path,
        typer.Option(
            "--graph-config",
            help="Path to the private Graph write-authorization YAML file.",
        ),
    ] = DEFAULT_GRAPH_WRITE_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Reconcile exactly one uncertain write using one Graph GET and zero PATCH requests."""

    try:
        settings = load_graph_write_settings(graph_config_path)
        settings.require_enabled()
        provider = GraphTokenProvider.from_settings(settings, PROJECT_ROOT)
        token = provider.acquire_silent()
        repository = ActionQueueRepository(queue_path)
        audit_log = ActionAuditLog(audit_path)
        with httpx.Client() as http_client:
            graph_client = GraphCategoryWriteClient(settings, http_client)
            report = UncertainActionReconciler(
                repository,
                graph_client,
                audit_log,
            ).reconcile(action_id, idempotency_key, token)
    except (
        GraphSettingsError,
        GraphWriteDisabledError,
        GraphAuthenticationError,
        GraphRequestError,
        ActionQueueStorageError,
        ActionAuditStorageError,
        ActionExecutionGuardError,
        ActionExecutionPersistenceError,
        ActionExecutionAuditError,
        ValidationError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    _render_action_reconciliation(report, output_format)
    if report.outcome in {
        ActionReconciliationOutcome.CONFLICT,
        ActionReconciliationOutcome.READ_FAILED,
    }:
        raise typer.Exit(code=2)


@outlook_app.command("login")
def outlook_login(
    graph_config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to a local Microsoft Graph YAML file."),
    ] = DEFAULT_GRAPH_PATH,
) -> None:
    """Sign in to personal Outlook with delegated Mail.Read device code."""

    try:
        settings = load_graph_settings(graph_config_path)
        provider = GraphTokenProvider.from_settings(settings, PROJECT_ROOT)
        token = provider.login(typer.echo)
    except (GraphSettingsError, GraphAuthenticationError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    account = token.username or "personal Microsoft account"
    typer.echo(f"Outlook delegated login succeeded for {account}.")
    typer.echo("Granted connector scope: Mail.Read (read-only).")


@outlook_app.command("write-login")
def outlook_write_login(
    graph_config_path: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            help="Path to a private Microsoft Graph write-authorization YAML file.",
        ),
    ] = DEFAULT_GRAPH_WRITE_PATH,
) -> None:
    """Authorize isolated delegated Mail.ReadWrite without changing mailbox data."""

    try:
        settings = load_graph_write_settings(graph_config_path)
        settings.require_enabled()
        provider = GraphTokenProvider.from_settings(settings, PROJECT_ROOT)
        token = provider.login(typer.echo)
    except (GraphSettingsError, GraphWriteDisabledError, GraphAuthenticationError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    account = token.username or "personal Microsoft account"
    typer.echo(f"Outlook delegated write authorization succeeded for {account}.")
    typer.echo("Granted connector scope: Mail.ReadWrite (delegated).")
    typer.echo("No Microsoft Graph mailbox write request was sent.")


@outlook_app.command("sync")
def outlook_sync(
    graph_config_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to a local Microsoft Graph YAML file."),
    ] = DEFAULT_GRAPH_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Synchronize personal Outlook Inbox changes using read-only Graph GET requests."""

    try:
        settings = load_graph_settings(graph_config_path)
        provider = GraphTokenProvider.from_settings(settings, PROJECT_ROOT)
        token = provider.acquire_silent()
        with httpx.Client() as http_client:
            client = GraphMailClient(settings, http_client)
            report = GraphInboxSynchronizer(settings, client, PROJECT_ROOT).sync(token)
    except (
        GraphSettingsError,
        GraphAuthenticationError,
        GraphRequestError,
        GraphSyncStorageError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    if output_format is OutputFormat.JSON:
        typer.echo(
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _render_graph_sync_table(report)

    if not report.completed:
        raise typer.Exit(code=2)


@app.command("validate-llm")
def validate_llm(
    llm_config_path: Annotated[
        Path,
        typer.Option("--llm-config", help="Path to a local OpenAI/DeepSeek provider YAML file."),
    ],
    dataset_path: Annotated[
        Path,
        typer.Option("--dataset", "-d", help="Path to the public validation dataset."),
    ] = DEFAULT_DATASET_PATH,
    expected_path: Annotated[
        Path,
        typer.Option("--labels", "-l", help="Path to independent classification labels."),
    ] = DEFAULT_EXPECTED_PATH,
    policy_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to the deterministic rule policy."),
    ] = DEFAULT_POLICY_PATH,
    llm_fusion_path: Annotated[
        Path,
        typer.Option("--llm-fusion-config", help="Path to the LLM fusion YAML policy."),
    ] = DEFAULT_LLM_FUSION_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            min=1,
            help="Analyze at most this many messages for a low-cost smoke test.",
        ),
    ] = None,
) -> None:
    """Call a real provider for every public sample and report classification metrics."""

    try:
        dataset = load_dataset(dataset_path)
        expected = load_expected_results(expected_path)
        if limit is not None:
            dataset = dataset.model_copy(update={"messages": dataset.messages[:limit]})

        labels_by_id = {label.source_id: label for label in expected.labels}
        selected_ids = tuple(message.source_id for message in dataset.messages)
        missing_label_ids = tuple(
            message_id for message_id in selected_ids if message_id not in labels_by_id
        )
        if missing_label_ids:
            joined_ids = ", ".join(missing_label_ids[:5])
            raise ExpectedResultsLoadError(
                expected_path,
                f"Expected results have no labels for selected messages: {joined_ids}",
            )
        expected = expected.model_copy(
            update={"labels": tuple(labels_by_id[message_id] for message_id in selected_ids)}
        )

        provider = OpenAICompatibleProvider.from_yaml(llm_config_path)
        pipeline = OfflinePipeline.from_yaml(
            policy_path,
            llm_provider=provider,
            llm_router=LLMRouter.analyze_all(),
            llm_fusion_path=llm_fusion_path,
        )
        analysis = pipeline.analyze_dataset(dataset, stop_on_llm_failure=True)
        report = validate_llm_classifications(
            analysis,
            expected,
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            prompt_version=provider.prompt_version,
        )
    except (
        DatasetLoadError,
        ExpectedResultsLoadError,
        RulePolicyError,
        LLMProviderConfigurationError,
        LLMFusionPolicyError,
    ) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    if output_format is OutputFormat.JSON:
        typer.echo(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        _render_llm_validation_table(report)

    if report.provider_failure_count:
        raise typer.Exit(code=2)
    if not report.passed:
        raise typer.Exit(code=3)


@app.command()
def evaluate(
    dataset_path: Annotated[
        Path,
        typer.Option("--dataset", "-d", help="Path to the evaluated JSON dataset."),
    ] = DEFAULT_DATASET_PATH,
    expected_path: Annotated[
        Path,
        typer.Option("--labels", "-l", help="Path to human expected results."),
    ] = DEFAULT_EXPECTED_PATH,
    policy_path: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to a YAML rule policy."),
    ] = DEFAULT_POLICY_PATH,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format: table or json."),
    ] = OutputFormat.TABLE,
) -> None:
    """Evaluate predictions against independent human-authored labels."""

    try:
        analysis = analyze_file(dataset_path, policy_path)
        expected = load_expected_results(expected_path)
    except (DatasetLoadError, RulePolicyError, ExpectedResultsLoadError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    report = evaluate_analysis(analysis, expected)
    if output_format is OutputFormat.JSON:
        _render_evaluation_json(report)
    else:
        _render_evaluation_table(report)

    if not report.passed:
        raise typer.Exit(code=3)


if __name__ == "__main__":
    app()
