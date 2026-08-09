"""Public local-service status and execution contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator, model_validator

from inbox_agent.models import FrozenModel
from inbox_agent.workflow import WorkflowReport


class ServiceStatus(StrEnum):
    """Persisted local scheduler lifecycle states."""

    IDLE = "idle"
    RUNNING = "running"
    SLEEPING = "sleeping"
    BACKOFF = "backoff"
    STOPPED = "stopped"


class ServiceRunOutcome(StrEnum):
    """Outcome of one scheduled or manual workflow attempt."""

    SUCCEEDED = "succeeded"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"


class ServiceRunResult(FrozenModel):
    """One scheduler attempt with bounded failure context and retry delay."""

    service_name: str
    outcome: ServiceRunOutcome
    attempted_at: datetime
    workflow_report: WorkflowReport | None = None
    error_type: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=500)
    consecutive_failures: int = Field(ge=0)
    delay_seconds: int = Field(ge=0)
    next_run_at: datetime | None = None

    @field_validator("attempted_at", "next_run_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("service timestamps must include timezone information")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> ServiceRunResult:
        if self.outcome is ServiceRunOutcome.FAILED:
            if self.error_type is None or self.error_message is None:
                raise ValueError("failed service run requires bounded error details")
        elif self.workflow_report is None:
            raise ValueError("completed service run requires a workflow report")
        if self.consecutive_failures == 0 and self.outcome is not ServiceRunOutcome.SUCCEEDED:
            raise ValueError("non-successful service run must increment consecutive_failures")
        return self


class ServiceStatusReport(FrozenModel):
    """Read-only combination of OS-lock liveness and persisted scheduler state."""

    service_name: str
    config_path: Path
    database_path: Path
    lock_path: Path
    active: bool
    database_initialized: bool
    database_revision: str | None = None
    needs_upgrade: bool = False
    persisted_status: ServiceStatus | None = None
    pid: int | None = None
    started_at: datetime | None = None
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    next_run_at: datetime | None = None
    last_run_id: str | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=1_000)

    @field_validator(
        "started_at",
        "last_run_at",
        "last_success_at",
        "last_failure_at",
        "next_run_at",
    )
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("service status timestamps must include timezone information")
        return value
