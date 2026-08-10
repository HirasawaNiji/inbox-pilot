"""Stage 4 step 7 observability, redaction, statistics, and CLI tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from inbox_agent.cli import app
from inbox_agent.loader import load_dataset
from inbox_agent.models import LLMTokenUsage
from inbox_agent.observability import (
    EventOutcome,
    LLMPricingRate,
    ObservabilityEvent,
    ObservabilityRecorder,
    StructuredLogWriter,
    estimate_llm_cost,
    safe_message_hash,
    sanitize_mapping,
    sanitize_text,
)
from inbox_agent.observability.diagnostics import DoctorLevel, run_doctor
from inbox_agent.storage import Database, upgrade_database
from inbox_agent.workflow import WorkflowRuntimeSettings, execute_workflow

NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
RUN_ID = "run-00000000000000000000000000000001"
runner = CliRunner()


def test_redaction_removes_credentials_and_private_content(tmp_path: Path) -> None:
    log_path = tmp_path / "private" / "logs" / "events.jsonl"
    event = ObservabilityEvent(
        occurred_at=NOW,
        run_id=RUN_ID,
        component="workflow",
        operation="workflow_run",
        outcome=EventOutcome.FAILED,
        error_type="ProviderError",
        model_name="sk-this-is-a-top-level-secret",
        details={
            "authorization": "Bearer secret-access-token",
            "api_key": "sk-this-is-a-test-secret",
            "subject": "private subject",
            "body_preview": "private body",
            "safe_count": 3,
        },
    )
    StructuredLogWriter(log_path).write(event)

    content = log_path.read_text(encoding="utf-8")
    payload = json.loads(content)
    assert "secret-access-token" not in content
    assert "sk-this-is-a-test-secret" not in content
    assert "sk-this-is-a-top-level-secret" not in content
    assert "private subject" not in content
    assert "private body" not in content
    assert payload["details"]["authorization"] == "<redacted>"
    assert payload["details"]["safe_count"] == 3
    assert sanitize_text("Bearer abc.def") == "<redacted>"
    assert sanitize_mapping({"content": "mail", "count": 1}) == {
        "content": "<redacted>",
        "count": 1,
    }


def test_pricing_is_decimal_and_requires_matching_config() -> None:
    usage = LLMTokenUsage(input_tokens=1_000, cached_input_tokens=400, output_tokens=200)
    rate = LLMPricingRate(
        provider="deepseek",
        model_name="deepseek-v4-flash",
        input_usd_per_million=Decimal("1.00"),
        cached_input_usd_per_million=Decimal("0.25"),
        output_usd_per_million=Decimal("2.00"),
    )

    assert rate.estimate_microusd(usage) == 1_100
    assert (
        estimate_llm_cost(
            (rate,),
            provider="deepseek",
            model_name="deepseek-v4-flash",
            usage=usage,
        )
        == 1_100
    )
    assert estimate_llm_cost((rate,), provider="openai", model_name="gpt-test", usage=usage) is None


def test_recorder_supports_run_provider_statistics_and_message_trace(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox_pilot.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    recorder = ObservabilityRecorder(database)
    message_id = "opaque-provider-message-id"
    message_hash = safe_message_hash(message_id)
    try:
        recorder.record(
            ObservabilityEvent(
                occurred_at=NOW,
                run_id=RUN_ID,
                message_hash=message_hash,
                component="llm",
                operation="llm_call",
                outcome=EventOutcome.SUCCEEDED,
                duration_ms=125,
                provider="deepseek",
                model_name="deepseek-v4-flash",
                input_tokens=100,
                output_tokens=20,
                cached_input_tokens=10,
                estimated_cost_microusd=42,
            )
        )
        recorder.record(
            ObservabilityEvent(
                occurred_at=NOW,
                run_id=RUN_ID,
                message_hash=safe_message_hash("another-message"),
                component="llm",
                operation="llm_call",
                outcome=EventOutcome.FAILED,
                provider="deepseek",
                model_name="deepseek-v4-flash",
                error_type="ProviderUnavailable",
            )
        )
        recorder.record(
            ObservabilityEvent(
                occurred_at=NOW,
                run_id=RUN_ID,
                component="workflow",
                operation="workflow_run",
                outcome=EventOutcome.SUCCEEDED,
                duration_ms=500,
            )
        )

        stats = recorder.statistics(window_hours=24, now=NOW)
        trace = recorder.trace_message(message_hash)
    finally:
        database.dispose()

    assert stats.workflow_runs == 1
    assert stats.workflow_success_rate == 1
    assert stats.average_workflow_duration_ms == 500
    assert stats.latest_error_type == "ProviderUnavailable"
    assert len(stats.providers) == 1
    assert stats.providers[0].attempts == 2
    assert stats.providers[0].success_rate == 0.5
    assert stats.providers[0].input_tokens == 100
    assert stats.providers[0].estimated_cost_usd is None
    assert len(trace) == 1
    assert trace[0].message_hash == message_hash
    log_content = (database_path.parent / "logs" / "inbox-pilot.jsonl").read_text(encoding="utf-8")
    assert message_id not in log_content
    assert message_hash in log_content


def test_doctor_is_read_only_and_reports_missing_optional_service_config(tmp_path: Path) -> None:
    database_path = tmp_path / "project" / "data" / "private" / "inbox.sqlite3"
    upgrade_database(database_path)
    backup_dir = database_path.parent / "backups"
    report = run_doctor(
        database_path=database_path,
        service_config_path=tmp_path / "missing-service.yaml",
        project_root=tmp_path / "project",
        backup_dir=backup_dir,
    )

    assert report.healthy is True
    levels = {check.name: check.level for check in report.checks}
    assert levels["database_revision"] is DoctorLevel.OK
    assert levels["database_integrity"] is DoctorLevel.OK
    assert levels["service_config"] is DoctorLevel.WARNING
    assert levels["private_storage"] is DoctorLevel.OK


def test_stats_trace_and_doctor_cli_are_privacy_bounded(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    database_path = project_root / "data" / "private" / "inbox.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    raw_message_id = "provider-secret-message-id"
    try:
        ObservabilityRecorder(database).record(
            ObservabilityEvent(
                occurred_at=NOW,
                run_id=RUN_ID,
                message_hash=safe_message_hash(raw_message_id),
                component="workflow",
                operation="message_analysis",
                outcome=EventOutcome.SUCCEEDED,
            )
        )
    finally:
        database.dispose()

    stats_result = runner.invoke(
        app,
        ["stats", "--database", str(database_path), "--format", "json"],
    )
    trace_result = runner.invoke(
        app,
        ["trace", raw_message_id, "--database", str(database_path), "--format", "json"],
    )
    doctor_result = runner.invoke(
        app,
        [
            "doctor",
            "--database",
            str(database_path),
            "--service-config",
            str(tmp_path / "missing.yaml"),
            "--backup-dir",
            str(database_path.parent / "backups"),
            "--format",
            "json",
        ],
    )

    assert stats_result.exit_code == 0
    assert trace_result.exit_code == 0
    assert raw_message_id not in trace_result.stdout
    assert json.loads(trace_result.stdout)["event_count"] == 1
    assert doctor_result.exit_code == 2
    assert json.loads(doctor_result.stdout)["healthy"] is True


def test_real_workflow_instrumentation_counts_pending_actions_and_trace(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    full_dataset = load_dataset(root / "data" / "samples" / "sample_emails.json")
    dataset = full_dataset.model_copy(update={"messages": full_dataset.messages[:1]})
    dataset_path = tmp_path / "messages.json"
    dataset_path.write_text(
        json.dumps(dataset.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    database_path = tmp_path / "private" / "inbox.sqlite3"
    report = execute_workflow(
        WorkflowRuntimeSettings(
            project_root=root,
            dataset_path=dataset_path,
            database_path=database_path,
            action_queue_path=tmp_path / "private" / "actions.json",
            audit_log_path=tmp_path / "private" / "audit.jsonl",
            policy_path=root / "config" / "rules.yaml",
        )
    )
    database = Database(database_path)
    try:
        recorder = ObservabilityRecorder(database)
        stats = recorder.statistics(window_hours=24, now=report.finished_at)
        trace = recorder.trace_message(safe_message_hash(dataset.messages[0].source_id))
    finally:
        database.dispose()

    assert report.graph_write_request_count == 0
    assert stats.action_backlog == 1
    assert stats.workflow_runs == 1
    assert {event.operation for event in trace} == {
        "message_import",
        "message_analysis",
        "action_build",
    }
