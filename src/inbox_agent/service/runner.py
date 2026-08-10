"""Single-instance local scheduling with bounded exponential backoff."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from inbox_agent.actions.locking import ActionFileLock, ActionFileLockTimeoutError
from inbox_agent.observability import (
    EventOutcome,
    ObservabilityEvent,
    ObservabilityRecorder,
    sanitize_text,
)
from inbox_agent.service.config import ServiceSettings
from inbox_agent.service.models import (
    ServiceRunOutcome,
    ServiceRunResult,
    ServiceStatus,
)
from inbox_agent.storage import Database, ServiceStateRecord, ServiceStateRepository
from inbox_agent.workflow import WorkflowReport, WorkflowStatus

Clock = Callable[[], datetime]
WorkflowExecutor = Callable[[], WorkflowReport]
ResultCallback = Callable[[ServiceRunResult], None]
ResultProcessor = Callable[[ServiceRunResult], object]
Waiter = Callable[[float], bool]


class ServiceAlreadyRunningError(RuntimeError):
    """Raised when another process owns the scheduler lock."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ServiceRunner:
    """Run one workflow at a time and keep retry state durable in SQLite."""

    def __init__(
        self,
        *,
        settings: ServiceSettings,
        database: Database,
        lock_path: Path,
        execute_workflow: WorkflowExecutor,
        result_processor: ResultProcessor | None = None,
        clock: Clock = _utc_now,
        waiter: Waiter | None = None,
        observability: ObservabilityRecorder | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.lock_path = Path(lock_path)
        self.execute_workflow = execute_workflow
        self.result_processor = result_processor
        self.clock = clock
        self._stop_event = threading.Event()
        self.waiter = waiter or self._stop_event.wait
        self.repository = ServiceStateRepository(database)
        self.observability = observability

    def request_stop(self) -> None:
        """Wake the scheduler and let the active workflow finish before stopping."""

        self._stop_event.set()

    def run_once(self) -> ServiceRunResult:
        """Execute once under the same OS lock used by the long-running service."""

        try:
            with ActionFileLock(self.lock_path, timeout_seconds=0):
                result = self._execute_locked(scheduled=False)
                self._process_result(result)
                return result
        except ActionFileLockTimeoutError as error:
            raise ServiceAlreadyRunningError(
                f"another InboxPilot service holds the lock: {self.lock_path}"
            ) from error

    def serve(
        self,
        *,
        on_result: ResultCallback | None = None,
        max_runs: int | None = None,
    ) -> tuple[ServiceRunResult, ...]:
        """Run until Ctrl+C/stop, optionally bounding attempts for acceptance tests."""

        if max_runs is not None and max_runs < 1:
            raise ValueError("max_runs must be positive")
        results: list[ServiceRunResult] = []
        lock_acquired = False
        try:
            with ActionFileLock(self.lock_path, timeout_seconds=0):
                lock_acquired = True
                self._mark_started()
                if not self.settings.run_immediately:
                    delay = self.settings.interval_minutes * 60
                    self._mark_waiting(ServiceStatus.SLEEPING, delay)
                    if self.waiter(delay):
                        return ()
                while not self._stop_event.is_set():
                    result = self._execute_locked(scheduled=True)
                    self._process_result(result)
                    results.append(result)
                    if on_result is not None:
                        on_result(result)
                    if max_runs is not None and len(results) >= max_runs:
                        break
                    if self.waiter(result.delay_seconds):
                        break
        except ActionFileLockTimeoutError as error:
            raise ServiceAlreadyRunningError(
                f"another InboxPilot service holds the lock: {self.lock_path}"
            ) from error
        finally:
            if lock_acquired:
                self._mark_stopped()
        return tuple(results)

    def _process_result(self, result: ServiceRunResult) -> None:
        """Run optional post-workflow features without changing workflow outcome."""

        if self.result_processor is None:
            return
        try:
            self.result_processor(result)
        except Exception:  # noqa: BLE001 - optional alerts cannot fail the scheduler
            return

    def _execute_locked(self, *, scheduled: bool) -> ServiceRunResult:
        attempted_at = self.clock()
        previous = self.repository.get(self.settings.service_name)
        previous_failures = previous.consecutive_failures if previous is not None else 0
        self._save_state(
            status=ServiceStatus.RUNNING,
            started_at=(previous.started_at if previous is not None else attempted_at.isoformat()),
            last_run_at=attempted_at.isoformat(),
            last_success_at=previous.last_success_at if previous is not None else None,
            last_failure_at=previous.last_failure_at if previous is not None else None,
            next_run_at=None,
            last_run_id=previous.last_run_id if previous is not None else None,
            consecutive_failures=previous_failures,
            last_error=None,
        )
        try:
            report = self.execute_workflow()
        except Exception as error:  # noqa: BLE001 - scheduler must persist and retry failures
            finished_at = self.clock()
            failures = previous_failures + 1
            delay = self._retry_delay(failures) if scheduled else 0
            next_run_at = finished_at + timedelta(seconds=delay) if scheduled else None
            self._save_state(
                status=ServiceStatus.BACKOFF if scheduled else ServiceStatus.IDLE,
                started_at=(
                    previous.started_at if previous is not None else attempted_at.isoformat()
                ),
                last_run_at=attempted_at.isoformat(),
                last_success_at=previous.last_success_at if previous is not None else None,
                last_failure_at=finished_at.isoformat(),
                next_run_at=next_run_at.isoformat() if next_run_at is not None else None,
                last_run_id=previous.last_run_id if previous is not None else None,
                consecutive_failures=failures,
                last_error=sanitize_text(
                    f"{type(error).__name__}: {error}",
                    maximum_length=900,
                ),
                pid=os.getpid() if scheduled else None,
            )
            result = ServiceRunResult(
                service_name=self.settings.service_name,
                outcome=ServiceRunOutcome.FAILED,
                attempted_at=attempted_at,
                error_type=type(error).__name__,
                error_message=str(error)[:500] or "Unknown workflow error",
                consecutive_failures=failures,
                delay_seconds=delay,
                next_run_at=next_run_at,
            )
            self._record_attempt(result, finished_at)
            return result

        finished_at = self.clock()
        successful = report.status is WorkflowStatus.COMPLETED
        failures = 0 if successful else previous_failures + 1
        delay = (
            (self.settings.interval_minutes * 60 if successful else self._retry_delay(failures))
            if scheduled
            else 0
        )
        next_run_at = finished_at + timedelta(seconds=delay) if scheduled else None
        outcome = (
            ServiceRunOutcome.SUCCEEDED if successful else ServiceRunOutcome.COMPLETED_WITH_FAILURES
        )
        self._save_state(
            status=(
                ServiceStatus.SLEEPING
                if successful and scheduled
                else ServiceStatus.BACKOFF
                if scheduled
                else ServiceStatus.IDLE
            ),
            started_at=(previous.started_at if previous is not None else attempted_at.isoformat()),
            last_run_at=attempted_at.isoformat(),
            last_success_at=(
                finished_at.isoformat()
                if successful
                else previous.last_success_at
                if previous is not None
                else None
            ),
            last_failure_at=(
                finished_at.isoformat()
                if not successful
                else previous.last_failure_at
                if previous is not None
                else None
            ),
            next_run_at=next_run_at.isoformat() if next_run_at is not None else None,
            last_run_id=report.run_id,
            consecutive_failures=failures,
            last_error=None if successful else "workflow completed with isolated failures",
            pid=os.getpid() if scheduled else None,
        )
        result = ServiceRunResult(
            service_name=self.settings.service_name,
            outcome=outcome,
            attempted_at=attempted_at,
            workflow_report=report,
            consecutive_failures=failures,
            delay_seconds=delay,
            next_run_at=next_run_at,
        )
        self._record_attempt(result, finished_at)
        return result

    def _record_attempt(self, result: ServiceRunResult, finished_at: datetime) -> None:
        if self.observability is None:
            return
        outcome = (
            EventOutcome.SUCCEEDED
            if result.outcome is ServiceRunOutcome.SUCCEEDED
            else EventOutcome.COMPLETED_WITH_FAILURES
            if result.outcome is ServiceRunOutcome.COMPLETED_WITH_FAILURES
            else EventOutcome.FAILED
        )
        try:
            self.observability.record(
                ObservabilityEvent(
                    occurred_at=finished_at,
                    run_id=(
                        result.workflow_report.run_id
                        if result.workflow_report is not None
                        else None
                    ),
                    component="service",
                    operation="service_attempt",
                    outcome=outcome,
                    duration_ms=max(
                        0,
                        round((finished_at - result.attempted_at).total_seconds() * 1_000),
                    ),
                    error_type=result.error_type,
                    details={
                        "service_name": result.service_name,
                        "consecutive_failures": result.consecutive_failures,
                        "delay_seconds": result.delay_seconds,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - telemetry cannot alter scheduler outcomes
            return

    def _retry_delay(self, consecutive_failures: int) -> int:
        base = self.settings.interval_minutes * 60
        maximum = self.settings.max_backoff_minutes * 60
        exponent = min(max(consecutive_failures - 1, 0), 20)
        multiplier = 1 << exponent
        return min(base * multiplier, maximum)

    def _mark_started(self) -> None:
        now = self.clock()
        previous = self.repository.get(self.settings.service_name)
        self._save_state(
            status=ServiceStatus.RUNNING,
            started_at=now.isoformat(),
            last_run_at=previous.last_run_at if previous is not None else None,
            last_success_at=previous.last_success_at if previous is not None else None,
            last_failure_at=previous.last_failure_at if previous is not None else None,
            next_run_at=None,
            last_run_id=previous.last_run_id if previous is not None else None,
            consecutive_failures=(previous.consecutive_failures if previous is not None else 0),
            last_error=previous.last_error if previous is not None else None,
        )

    def _mark_waiting(self, status: ServiceStatus, delay_seconds: int) -> None:
        now = self.clock()
        previous = self.repository.get(self.settings.service_name)
        assert previous is not None
        self._save_state(
            status=status,
            started_at=previous.started_at,
            last_run_at=previous.last_run_at,
            last_success_at=previous.last_success_at,
            last_failure_at=previous.last_failure_at,
            next_run_at=(now + timedelta(seconds=delay_seconds)).isoformat(),
            last_run_id=previous.last_run_id,
            consecutive_failures=previous.consecutive_failures,
            last_error=previous.last_error,
        )

    def _mark_stopped(self) -> None:
        previous = self.repository.get(self.settings.service_name)
        if previous is None:
            return
        self._save_state(
            status=ServiceStatus.STOPPED,
            started_at=previous.started_at,
            last_run_at=previous.last_run_at,
            last_success_at=previous.last_success_at,
            last_failure_at=previous.last_failure_at,
            next_run_at=None,
            last_run_id=previous.last_run_id,
            consecutive_failures=previous.consecutive_failures,
            last_error=previous.last_error,
            pid=None,
        )

    def _save_state(
        self,
        *,
        status: ServiceStatus,
        started_at: str | None,
        last_run_at: str | None,
        last_success_at: str | None,
        last_failure_at: str | None,
        next_run_at: str | None,
        last_run_id: str | None,
        consecutive_failures: int,
        last_error: str | None,
        pid: int | None = os.getpid(),
    ) -> None:
        self.repository.save(
            ServiceStateRecord(
                service_name=self.settings.service_name,
                status=status.value,
                pid=pid,
                started_at=started_at,
                last_run_at=last_run_at,
                last_success_at=last_success_at,
                last_failure_at=last_failure_at,
                next_run_at=next_run_at,
                last_run_id=last_run_id,
                consecutive_failures=consecutive_failures,
                last_error=last_error,
                updated_at=self.clock().isoformat(),
            )
        )
