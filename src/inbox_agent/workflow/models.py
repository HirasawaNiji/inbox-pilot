"""Public contracts for InboxPilot's durable Stage 4 workflow."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from inbox_agent.models import FrozenModel


class WorkflowStatus(StrEnum):
    """Terminal and non-terminal workflow states."""

    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"


class WorkflowStepStatus(StrEnum):
    """Lifecycle states for one durable workflow step."""

    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class WorkflowFailure(FrozenModel):
    """Bounded per-message or synchronization failure."""

    message_id: str = Field(min_length=1, max_length=512)
    stage: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    error_type: str = Field(min_length=1, max_length=100)
    error_message: str = Field(min_length=1, max_length=500)


class WorkflowStep(FrozenModel):
    """One persisted workflow step suitable for status display and recovery."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    status: WorkflowStepStatus
    started_at: datetime
    finished_at: datetime | None = None
    processed_count: int = Field(default=0, ge=0)
    detail: str | None = Field(default=None, max_length=500)

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("workflow step timestamps must include timezone information")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> WorkflowStep:
        if self.status is WorkflowStepStatus.RUNNING and self.finished_at is not None:
            raise ValueError("a running step cannot have finished_at")
        if self.status is not WorkflowStepStatus.RUNNING and self.finished_at is None:
            raise ValueError("a terminal step requires finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("workflow step finished_at cannot precede started_at")
        return self


class DatasetSyncResult(FrozenModel):
    """Provider-neutral result returned by an optional read-only synchronization."""

    dataset_path: Path
    completed: bool
    created_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    removed_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)


class WorkflowReport(FrozenModel):
    """Stable result of one complete synchronization-to-review workflow."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(pattern=r"^run-[a-f0-9]{32}$")
    status: WorkflowStatus
    started_at: datetime
    finished_at: datetime
    dataset_path: Path
    database_path: Path
    analysis_profile: str = Field(pattern=r"^[a-f0-9]{64}$")
    llm_provider: str | None = Field(default=None, max_length=100)
    outlook_sync_requested: bool = False

    total_messages: int = Field(ge=0)
    imported_created: int = Field(ge=0)
    imported_updated: int = Field(ge=0)
    imported_unchanged: int = Field(ge=0)
    eligible_messages: int = Field(ge=0)
    skipped_current: int = Field(ge=0)
    analyzed_messages: int = Field(ge=0)
    persisted_analyses: int = Field(ge=0)
    analysis_failures: tuple[WorkflowFailure, ...] = ()
    llm_failures: tuple[WorkflowFailure, ...] = ()

    actions_generated: int = Field(ge=0)
    actions_added: int = Field(ge=0)
    actions_skipped: int = Field(ge=0)
    audit_events_added: int = Field(ge=0)
    graph_write_request_count: Literal[0] = 0
    steps: tuple[WorkflowStep, ...]

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("workflow timestamps must include timezone information")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> WorkflowReport:
        imported = self.imported_created + self.imported_updated + self.imported_unchanged
        if imported != self.total_messages:
            raise ValueError("import counts must equal total_messages")
        if self.eligible_messages + self.skipped_current > self.total_messages:
            raise ValueError("eligible and skipped counts cannot exceed total_messages")
        if self.finished_at < self.started_at:
            raise ValueError("workflow finished_at cannot precede started_at")
        return self
