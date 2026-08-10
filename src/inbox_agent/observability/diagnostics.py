"""Read-only diagnostics for local configuration, storage, and scheduler health."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from inbox_agent.models import FrozenModel
from inbox_agent.service import inspect_service, load_service_settings
from inbox_agent.storage import Database, current_revision, head_revision


class DoctorLevel(StrEnum):
    """Severity levels with deterministic CLI exit semantics."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


class DoctorCheck(FrozenModel):
    """One bounded diagnostic result without private payloads."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    level: DoctorLevel
    summary: str = Field(min_length=1, max_length=500)


class DoctorReport(FrozenModel):
    """Complete read-only local health report."""

    healthy: bool
    checks: tuple[DoctorCheck, ...]


def run_doctor(
    *,
    database_path: Path,
    service_config_path: Path,
    project_root: Path,
    backup_dir: Path,
) -> DoctorReport:
    """Inspect local prerequisites without creating, migrating, or contacting providers."""

    checks: list[DoctorCheck] = []
    resolved_database = database_path.resolve()
    if not resolved_database.is_file():
        checks.append(
            DoctorCheck(
                name="database", level=DoctorLevel.ERROR, summary="database file is missing"
            )
        )
    else:
        database = Database(resolved_database)
        try:
            revision = current_revision(database.engine)
            expected = head_revision(resolved_database)
            checks.append(
                DoctorCheck(
                    name="database_revision",
                    level=DoctorLevel.OK if revision == expected else DoctorLevel.ERROR,
                    summary=f"revision={revision or 'uninitialized'}, expected={expected}",
                )
            )
            with database.engine.connect() as connection:
                integrity = connection.exec_driver_sql("PRAGMA quick_check").scalar_one()
            checks.append(
                DoctorCheck(
                    name="database_integrity",
                    level=DoctorLevel.OK if integrity == "ok" else DoctorLevel.ERROR,
                    summary="SQLite quick_check passed"
                    if integrity == "ok"
                    else "SQLite quick_check failed",
                )
            )
        finally:
            database.dispose()

    if not service_config_path.is_file():
        checks.append(
            DoctorCheck(
                name="service_config",
                level=DoctorLevel.WARNING,
                summary="service configuration is missing; CLI-only operation remains available",
            )
        )
    else:
        settings = load_service_settings(service_config_path)
        service = inspect_service(
            settings, config_path=service_config_path, project_root=project_root
        )
        checks.append(
            DoctorCheck(
                name="service_lock",
                level=DoctorLevel.OK,
                summary="scheduler is active" if service.active else "scheduler is inactive",
            )
        )
        if service.last_error:
            checks.append(
                DoctorCheck(
                    name="last_service_error",
                    level=DoctorLevel.WARNING,
                    summary="scheduler has a bounded recent error; inspect service status",
                )
            )
        runtime = settings.runtime_settings(project_root)
        log_parent = (
            runtime.observability_log_path.parent
            if runtime.observability_log_path is not None
            else resolved_database.parent / "logs"
        )
        checks.append(_directory_check("log_directory", log_parent))

    checks.append(_directory_check("backup_directory", backup_dir.resolve()))
    private_root = (project_root / "data" / "private").resolve()
    checks.append(
        DoctorCheck(
            name="private_storage",
            level=(
                DoctorLevel.OK
                if resolved_database.is_relative_to(private_root)
                else DoctorLevel.WARNING
            ),
            summary=(
                "database is under data/private"
                if resolved_database.is_relative_to(private_root)
                else "database is outside the recommended data/private directory"
            ),
        )
    )
    return DoctorReport(
        healthy=not any(check.level is DoctorLevel.ERROR for check in checks),
        checks=tuple(checks),
    )


def _directory_check(name: str, path: Path) -> DoctorCheck:
    probe = path if path.exists() else path.parent
    available = probe.is_dir() and os.access(probe, os.W_OK)
    return DoctorCheck(
        name=name,
        level=DoctorLevel.OK if available else DoctorLevel.ERROR,
        summary=(
            f"writable location available: {path}"
            if available
            else f"location is not writable: {path}"
        ),
    )
