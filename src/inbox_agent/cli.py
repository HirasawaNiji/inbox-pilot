"""Command-line interface for the offline InboxPilot workflow."""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from inbox_agent.evaluation import (
    EvaluationReport,
    ExpectedResultsLoadError,
    evaluate_analysis,
    load_expected_results,
)
from inbox_agent.loader import DatasetLoadError
from inbox_agent.models import TriageResult
from inbox_agent.pipeline import AnalysisReport, analyze_file
from inbox_agent.rule_engine import RulePolicyError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET_PATH = PROJECT_ROOT / "data" / "samples" / "sample_emails.json"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "config" / "rules.yaml"
DEFAULT_EXPECTED_PATH = PROJECT_ROOT / "data" / "eval" / "expected_results.json"


class OutputFormat(StrEnum):
    """Supported CLI output representations."""

    TABLE = "table"
    JSON = "json"


app = typer.Typer(
    name="inbox-agent",
    help="Analyze email priority with explainable offline rules.",
    invoke_without_command=True,
)


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


def _run(
    dataset_path: Path,
    policy_path: Path,
    output_format: OutputFormat,
    show_reasons: bool,
) -> None:
    """Execute one CLI analysis with consistent error and exit handling."""

    try:
        report = analyze_file(dataset_path, policy_path)
    except (DatasetLoadError, RulePolicyError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error

    if output_format is OutputFormat.JSON:
        _render_json(report)
    else:
        _render_table(report, show_reasons=show_reasons)

    if report.failure_count:
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
) -> None:
    """Analyze a JSON email dataset with an explainable YAML policy."""

    _run(dataset_path, policy_path, output_format, show_reasons)


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
