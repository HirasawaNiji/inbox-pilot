"""Stage 4 single-instance scheduler, backoff, and status tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from inbox_agent.actions.locking import ActionFileLock
from inbox_agent.service import (
    ServiceAlreadyRunningError,
    ServiceRunner,
    ServiceRunOutcome,
    ServiceRunResult,
    ServiceSettings,
    ServiceStatus,
    ServiceWorkflowSettings,
    inspect_service,
    load_service_settings,
)
from inbox_agent.storage import Database, ServiceStateRepository, upgrade_database
from inbox_agent.workflow import WorkflowReport, WorkflowStatus

FIXED_TIME = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def workflow_report(
    status: WorkflowStatus = WorkflowStatus.COMPLETED,
    *,
    run_id: str = "run-00000000000000000000000000000001",
) -> WorkflowReport:
    return WorkflowReport(
        run_id=run_id,
        status=status,
        started_at=FIXED_TIME,
        finished_at=FIXED_TIME,
        dataset_path=Path("messages.json"),
        database_path=Path("inbox_pilot.sqlite3"),
        analysis_profile="a" * 64,
        total_messages=0,
        imported_created=0,
        imported_updated=0,
        imported_unchanged=0,
        eligible_messages=0,
        skipped_current=0,
        analyzed_messages=0,
        persisted_analyses=0,
        actions_generated=0,
        actions_added=0,
        actions_skipped=0,
        audit_events_added=0,
        steps=(),
    )


def service_settings(tmp_path: Path, **updates: object) -> ServiceSettings:
    workflow = ServiceWorkflowSettings(
        dataset_path=tmp_path / "messages.json",
        database_path=tmp_path / "inbox_pilot.sqlite3",
        action_queue_path=tmp_path / "actions.json",
        audit_log_path=tmp_path / "audit.jsonl",
    )
    return ServiceSettings(
        interval_minutes=1,
        max_backoff_minutes=4,
        lock_path=tmp_path / "service.lock",
        workflow=workflow,
        **updates,
    )


def migrated_database(settings: ServiceSettings, tmp_path: Path) -> Database:
    path = settings.workflow.runtime_settings(tmp_path).database_path
    upgrade_database(path)
    return Database(path)


def test_service_configuration_loads_safe_isolated_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "service.local.yaml"
    config_path.write_text(
        """
schema_version: "1.0"
service_name: test-agent
interval_minutes: 5
max_backoff_minutes: 20
run_immediately: true
lock_path: data/private/test.lock
workflow:
  dataset_path: data/samples/sample_emails.json
  database_path: data/private/test.sqlite3
  action_queue_path: data/private/test-actions.json
  audit_log_path: data/private/test-audit.jsonl
  policy_path: config/rules.yaml
  sync_outlook: false
""".strip(),
        encoding="utf-8",
    )

    settings = load_service_settings(config_path)
    runtime = settings.workflow.runtime_settings(tmp_path)

    assert settings.service_name == "test-agent"
    assert settings.interval_minutes == 5
    assert runtime.database_path == (tmp_path / "data/private/test.sqlite3").resolve()
    assert runtime.sync_outlook is False
    assert runtime.llm_config_path is None


def test_run_once_persists_success_and_releases_process_identity(tmp_path: Path) -> None:
    settings = service_settings(tmp_path)
    database = migrated_database(settings, tmp_path)
    processed: list[ServiceRunResult] = []
    runner = ServiceRunner(
        settings=settings,
        database=database,
        lock_path=settings.resolved_lock_path(tmp_path),
        execute_workflow=workflow_report,
        result_processor=processed.append,
        clock=lambda: FIXED_TIME,
    )
    try:
        result = runner.run_once()
        state = ServiceStateRepository(database).get(settings.service_name)

        assert result.outcome is ServiceRunOutcome.SUCCEEDED
        assert result.delay_seconds == 0
        assert result.workflow_report is not None
        assert result.workflow_report.graph_write_request_count == 0
        assert state is not None
        assert state.status == ServiceStatus.IDLE.value
        assert state.pid is None
        assert state.last_success_at == FIXED_TIME.isoformat()
        assert state.consecutive_failures == 0
        assert processed == [result]
    finally:
        database.dispose()


def test_scheduler_applies_bounded_exponential_backoff_then_resets(tmp_path: Path) -> None:
    settings = service_settings(tmp_path)
    database = migrated_database(settings, tmp_path)
    attempts = 0
    waits: list[float] = []

    def execute() -> WorkflowReport:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError(f"temporary failure {attempts}")
        return workflow_report(run_id="run-00000000000000000000000000000003")

    def wait(delay: float) -> bool:
        waits.append(delay)
        return False

    runner = ServiceRunner(
        settings=settings,
        database=database,
        lock_path=settings.resolved_lock_path(tmp_path),
        execute_workflow=execute,
        clock=lambda: FIXED_TIME,
        waiter=wait,
    )
    try:
        results = runner.serve(max_runs=3)
        state = ServiceStateRepository(database).get(settings.service_name)

        assert [result.outcome for result in results] == [
            ServiceRunOutcome.FAILED,
            ServiceRunOutcome.FAILED,
            ServiceRunOutcome.SUCCEEDED,
        ]
        assert [result.delay_seconds for result in results] == [60, 120, 60]
        assert waits == [60, 120]
        assert state is not None
        assert state.status == ServiceStatus.STOPPED.value
        assert state.consecutive_failures == 0
        assert state.last_run_id is not None and state.last_run_id.endswith("03")
    finally:
        database.dispose()


def test_completed_with_failures_uses_backoff_and_retries(tmp_path: Path) -> None:
    settings = service_settings(tmp_path)
    database = migrated_database(settings, tmp_path)
    runner = ServiceRunner(
        settings=settings,
        database=database,
        lock_path=settings.resolved_lock_path(tmp_path),
        execute_workflow=lambda: workflow_report(WorkflowStatus.COMPLETED_WITH_FAILURES),
        clock=lambda: FIXED_TIME,
        waiter=lambda _: True,
    )
    try:
        results = runner.serve(max_runs=2)

        assert len(results) == 1
        assert results[0].outcome is ServiceRunOutcome.COMPLETED_WITH_FAILURES
        assert results[0].consecutive_failures == 1
        assert results[0].delay_seconds == 60
    finally:
        database.dispose()


def test_os_lock_prevents_second_runner_without_overwriting_state(tmp_path: Path) -> None:
    settings = service_settings(tmp_path)
    database = migrated_database(settings, tmp_path)
    lock_path = settings.resolved_lock_path(tmp_path)
    runner = ServiceRunner(
        settings=settings,
        database=database,
        lock_path=lock_path,
        execute_workflow=workflow_report,
        clock=lambda: FIXED_TIME,
    )
    try:
        with ActionFileLock(lock_path, timeout_seconds=0):
            with pytest.raises(ServiceAlreadyRunningError):
                runner.run_once()
            with pytest.raises(ServiceAlreadyRunningError):
                runner.serve(max_runs=1)

        assert ServiceStateRepository(database).get(settings.service_name) is None
    finally:
        database.dispose()


def test_status_combines_live_lock_and_persisted_state(tmp_path: Path) -> None:
    settings = service_settings(tmp_path)
    database = migrated_database(settings, tmp_path)
    lock_path = settings.resolved_lock_path(tmp_path)
    runner = ServiceRunner(
        settings=settings,
        database=database,
        lock_path=lock_path,
        execute_workflow=workflow_report,
        clock=lambda: FIXED_TIME,
    )
    config_path = tmp_path / "service.local.yaml"
    try:
        runner.run_once()
        inactive = inspect_service(settings, config_path=config_path, project_root=tmp_path)
        with ActionFileLock(lock_path, timeout_seconds=0):
            active = inspect_service(settings, config_path=config_path, project_root=tmp_path)

        assert inactive.active is False
        assert inactive.persisted_status is ServiceStatus.IDLE
        assert active.active is True
        assert active.database_revision == "0005_observability"
        assert active.needs_upgrade is False
    finally:
        database.dispose()


def test_status_does_not_create_missing_database_or_lock(tmp_path: Path) -> None:
    settings = service_settings(tmp_path)
    database_path = settings.workflow.runtime_settings(tmp_path).database_path
    lock_path = settings.resolved_lock_path(tmp_path)

    report = inspect_service(
        settings,
        config_path=tmp_path / "service.local.yaml",
        project_root=tmp_path,
    )

    assert report.active is False
    assert report.database_initialized is False
    assert not database_path.exists()
    assert not lock_path.exists()
