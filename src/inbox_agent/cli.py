"""Command-line interface for offline rules and optional LLM analysis."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table

from inbox_agent.evaluation import (
    EvaluationReport,
    ExpectedResultsLoadError,
    evaluate_analysis,
    load_expected_results,
)
from inbox_agent.graph import (
    GraphAuthenticationError,
    GraphInboxSynchronizer,
    GraphMailClient,
    GraphRequestError,
    GraphSettingsError,
    GraphSyncReport,
    GraphSyncStorageError,
    GraphTokenProvider,
    load_graph_settings,
)
from inbox_agent.llm import (
    LLMFusionPolicyError,
    LLMProviderConfigurationError,
    LLMRoutingPolicyError,
    OpenAICompatibleProvider,
)
from inbox_agent.loader import DatasetLoadError
from inbox_agent.models import TriageResult
from inbox_agent.pipeline import AnalysisReport, analyze_file
from inbox_agent.rule_engine import RulePolicyError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET_PATH = PROJECT_ROOT / "data" / "samples" / "sample_emails.json"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "config" / "rules.yaml"
DEFAULT_EXPECTED_PATH = PROJECT_ROOT / "data" / "eval" / "expected_results.json"
DEFAULT_LLM_ROUTING_PATH = PROJECT_ROOT / "config" / "llm_routing.yaml"
DEFAULT_LLM_FUSION_PATH = PROJECT_ROOT / "config" / "llm_fusion.yaml"
DEFAULT_GRAPH_PATH = PROJECT_ROOT / "config" / "graph.local.yaml"


class OutputFormat(StrEnum):
    """Supported CLI output representations."""

    TABLE = "table"
    JSON = "json"


app = typer.Typer(
    name="inbox-agent",
    help="Analyze email priority with explainable rules and an optional LLM.",
    invoke_without_command=True,
)
outlook_app = typer.Typer(help="Authenticate and synchronize a personal Outlook Inbox read-only.")
app.add_typer(outlook_app, name="outlook")


def _console() -> Console:
    """Create a console at invocation time so test runners can capture output."""

    return Console(highlight=False)


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
