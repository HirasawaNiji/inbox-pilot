"""Read-only scheduler liveness and persisted-state inspection."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from inbox_agent.actions.locking import ActionFileLock, ActionFileLockTimeoutError
from inbox_agent.service.config import ServiceSettings
from inbox_agent.service.models import ServiceStatus, ServiceStatusReport
from inbox_agent.storage import (
    Database,
    ServiceStateRepository,
    current_revision,
    head_revision,
)


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def service_is_active(lock_path: Path) -> bool:
    """Probe the OS lock without trusting a potentially stale PID."""

    if not lock_path.exists():
        return False
    try:
        with ActionFileLock(lock_path, timeout_seconds=0):
            return False
    except ActionFileLockTimeoutError:
        return True


def inspect_service(
    settings: ServiceSettings,
    *,
    config_path: Path,
    project_root: Path,
) -> ServiceStatusReport:
    """Inspect without creating or migrating a missing database."""

    runtime = settings.workflow.runtime_settings(project_root)
    lock_path = settings.resolved_lock_path(project_root)
    latest_revision = head_revision(runtime.database_path)
    active = service_is_active(lock_path)
    if not runtime.database_path.is_file():
        return ServiceStatusReport(
            service_name=settings.service_name,
            config_path=config_path.resolve(),
            database_path=runtime.database_path,
            lock_path=lock_path,
            active=active,
            database_initialized=False,
            database_revision=None,
        )

    database = Database(runtime.database_path)
    try:
        revision = current_revision(database.engine)
        state = (
            ServiceStateRepository(database).get(settings.service_name)
            if revision == latest_revision
            else None
        )
    finally:
        database.dispose()
    return ServiceStatusReport(
        service_name=settings.service_name,
        config_path=config_path.resolve(),
        database_path=runtime.database_path,
        lock_path=lock_path,
        active=active,
        database_initialized=revision is not None,
        database_revision=revision,
        needs_upgrade=revision is not None and revision != latest_revision,
        persisted_status=ServiceStatus(state.status) if state is not None else None,
        pid=state.pid if state is not None else None,
        started_at=_datetime(state.started_at) if state is not None else None,
        last_run_at=_datetime(state.last_run_at) if state is not None else None,
        last_success_at=_datetime(state.last_success_at) if state is not None else None,
        last_failure_at=_datetime(state.last_failure_at) if state is not None else None,
        next_run_at=_datetime(state.next_run_at) if state is not None else None,
        last_run_id=state.last_run_id if state is not None else None,
        consecutive_failures=state.consecutive_failures if state is not None else 0,
        last_error=state.last_error if state is not None else None,
    )
