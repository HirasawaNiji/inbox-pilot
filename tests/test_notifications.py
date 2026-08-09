"""Stage 4 local notification, deduplication, and digest tests."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from inbox_agent.loader import load_dataset
from inbox_agent.models import Priority
from inbox_agent.notifications import (
    NotificationCoordinator,
    NotificationDeliveryRepository,
    NotificationDeliveryStatus,
    NotificationKind,
    RecordingDesktopNotifier,
    WindowsToastNotifier,
)
from inbox_agent.pipeline import OfflinePipeline
from inbox_agent.service import (
    ServiceNotificationSettings,
    ServiceRunOutcome,
    ServiceRunResult,
)
from inbox_agent.storage import AnalysisRepository, Database, MessageRepository, upgrade_database
from inbox_agent.workflow import WorkflowReport, WorkflowStatus

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
POLICY_PATH = ROOT / "config" / "rules.yaml"
FIXED_TIME = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def workflow_report(status: WorkflowStatus = WorkflowStatus.COMPLETED) -> WorkflowReport:
    return WorkflowReport(
        run_id="run-00000000000000000000000000000041",
        status=status,
        started_at=FIXED_TIME,
        finished_at=FIXED_TIME,
        dataset_path=Path("messages.json"),
        database_path=Path("inbox_pilot.sqlite3"),
        analysis_profile="a" * 64,
        total_messages=1,
        imported_created=1,
        imported_updated=0,
        imported_unchanged=0,
        eligible_messages=1,
        skipped_current=0,
        analyzed_messages=1,
        persisted_analyses=1,
        actions_generated=0,
        actions_added=0,
        actions_skipped=0,
        audit_events_added=0,
        steps=(),
    )


def success_result() -> ServiceRunResult:
    return ServiceRunResult(
        service_name="inbox-pilot-test",
        outcome=ServiceRunOutcome.SUCCEEDED,
        attempted_at=FIXED_TIME,
        workflow_report=workflow_report(),
        consecutive_failures=0,
        delay_seconds=0,
    )


def seed_priority_message(database: Database) -> str:
    message = load_dataset(DATASET_PATH).messages[0]
    private_body = "COMPLETE_PRIVATE_BODY_MUST_NOT_APPEAR_IN_NOTIFICATIONS"
    message = message.model_copy(
        update={
            "subject": "Private scholarship decision",
            "received_at": FIXED_TIME,
            "body": message.body.model_copy(update={"content": private_body}),
            "body_preview": private_body,
        }
    )
    dataset = load_dataset(DATASET_PATH).model_copy(update={"messages": (message,)})
    analysis = OfflinePipeline.from_yaml(POLICY_PATH).analyze_dataset(dataset)
    result = analysis.results[0].model_copy(
        update={
            "priority": Priority.P1,
            "summary": "Open the student portal before the deadline.",
            "deadline": FIXED_TIME + timedelta(hours=18),
            "requires_review": True,
            "evaluated_at": FIXED_TIME,
        }
    )
    MessageRepository(database).upsert(message)
    AnalysisRepository(database).save(
        source=message.source,
        result=result,
        rule_evaluation=analysis.rule_evaluations[0],
        analysis_profile="a" * 64,
    )
    return private_body


def test_notification_repository_deduplicates_and_bounds_retries(tmp_path: Path) -> None:
    database_path = tmp_path / "inbox.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    repository = NotificationDeliveryRepository(database, retry_limit=2)
    key = "a" * 64
    try:
        assert repository.claim(
            dedupe_key=key,
            kind=NotificationKind.PRIORITY_ALERT,
            attempted_at=FIXED_TIME,
        )
        assert not repository.claim(
            dedupe_key=key,
            kind=NotificationKind.PRIORITY_ALERT,
            attempted_at=FIXED_TIME,
        )
        repository.mark_failed(
            (key,),
            failed_at=FIXED_TIME,
            error_summary="DesktopNotificationError: desktop delivery failed",
        )
        assert repository.claim(
            dedupe_key=key,
            kind=NotificationKind.PRIORITY_ALERT,
            attempted_at=FIXED_TIME,
        )
        repository.mark_failed(
            (key,),
            failed_at=FIXED_TIME,
            error_summary="DesktopNotificationError: desktop delivery failed",
        )
        assert not repository.claim(
            dedupe_key=key,
            kind=NotificationKind.PRIORITY_ALERT,
            attempted_at=FIXED_TIME,
        )
        record = repository.get(key)
        assert record is not None
        assert record.status == NotificationDeliveryStatus.FAILED.value
        assert record.attempt_count == 2
    finally:
        database.dispose()


def test_priority_deadline_and_daily_summary_are_private_and_deduplicated(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "inbox.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    private_body = seed_priority_message(database)
    desktop = RecordingDesktopNotifier()
    coordinator = NotificationCoordinator(
        database=database,
        action_queue_path=tmp_path / "actions.json",
        output_dir=tmp_path / "summaries",
        settings=ServiceNotificationSettings(daily_summary_hour=0),
        desktop_notifier=desktop,
        clock=lambda: FIXED_TIME,
    )
    try:
        first = coordinator.process(success_result())
        second = coordinator.process(success_result())

        assert first.priority_alerts == 1
        assert first.deadline_alerts == 1
        assert first.summary_path == tmp_path / "summaries" / "2026-08-09.md"
        assert second.priority_alerts == 0
        assert second.deadline_alerts == 0
        assert second.summary_path is None
        assert len(desktop.messages) == 2
        desktop_text = "\n".join(value for pair in desktop.messages for value in pair)
        assert "Private scholarship decision" not in desktop_text
        assert private_body not in desktop_text

        summary = first.summary_path.read_text(encoding="utf-8")
        assert "Private scholarship decision" in summary
        assert "Open the student portal before the deadline." in summary
        assert "需要人工复核的邮件：**1**" in summary
        assert "等待批准或拒绝的动作：**0**" in summary
        assert private_body not in summary
    finally:
        database.dispose()


def test_workflow_failure_alert_is_generic_and_once_per_day(tmp_path: Path) -> None:
    database_path = tmp_path / "inbox.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    desktop = RecordingDesktopNotifier()
    coordinator = NotificationCoordinator(
        database=database,
        action_queue_path=tmp_path / "actions.json",
        output_dir=tmp_path / "summaries",
        settings=ServiceNotificationSettings(daily_summary_enabled=False),
        desktop_notifier=desktop,
        clock=lambda: FIXED_TIME,
    )
    failed = ServiceRunResult(
        service_name="inbox-pilot-test",
        outcome=ServiceRunOutcome.FAILED,
        attempted_at=FIXED_TIME,
        error_type="GraphAuthenticationError",
        error_message="secret provider response must not be shown",
        consecutive_failures=1,
        delay_seconds=300,
        next_run_at=FIXED_TIME + timedelta(minutes=5),
    )
    try:
        first = coordinator.process(failed)
        second = coordinator.process(failed)

        assert first.failure_alerts == 1
        assert second.failure_alerts == 0
        assert len(desktop.messages) == 1
        assert "secret provider response" not in desktop.messages[0][1]
    finally:
        database.dispose()


def test_desktop_failure_is_isolated_from_successful_workflow(tmp_path: Path) -> None:
    class FailingDesktopNotifier:
        def show(self, title: str, message: str) -> None:
            raise OSError("private operating-system response")

    database_path = tmp_path / "inbox.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    seed_priority_message(database)
    coordinator = NotificationCoordinator(
        database=database,
        action_queue_path=tmp_path / "actions.json",
        output_dir=tmp_path / "summaries",
        settings=ServiceNotificationSettings(daily_summary_enabled=False),
        desktop_notifier=FailingDesktopNotifier(),
        clock=lambda: FIXED_TIME,
    )
    service_result = success_result()
    try:
        report = coordinator.process(service_result)

        assert service_result.outcome is ServiceRunOutcome.SUCCEEDED
        assert report.priority_alerts == 0
        assert report.deadline_alerts == 0
        assert report.errors
        assert "private operating-system response" not in " ".join(report.errors)
    finally:
        database.dispose()


def test_windows_toast_passes_content_through_environment_not_script() -> None:
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    notifier = WindowsToastNotifier(runner=run, environment={})
    notifier.show("Private title", "Private notification message")

    command_text = " ".join(captured["command"])  # type: ignore[arg-type]
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert "Private title" not in command_text
    assert "Private notification message" not in command_text
    assert environment["INBOX_PILOT_TOAST_TITLE"] == "Private title"
    assert environment["INBOX_PILOT_TOAST_MESSAGE"] == "Private notification message"
