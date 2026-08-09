"""Public contracts for local alerts and daily summaries."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator

from inbox_agent.models import FrozenModel, Priority


class NotificationKind(StrEnum):
    """Stable event classes stored in the privacy-safe delivery ledger."""

    PRIORITY_ALERT = "priority_alert"
    DEADLINE_ALERT = "deadline_alert"
    WORKFLOW_FAILURE = "workflow_failure"
    DAILY_SUMMARY = "daily_summary"


class NotificationDeliveryStatus(StrEnum):
    """Lifecycle of one deduplicated delivery attempt."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class DigestItem(FrozenModel):
    """Minimal analyzed message projection used by alerts and summaries."""

    message_key: str = Field(min_length=1, max_length=1_000)
    analysis_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    subject: str = Field(max_length=1_000)
    priority: Priority
    summary: str = Field(min_length=1, max_length=1_000)
    received_at: datetime
    deadline: datetime | None = None
    requires_review: bool = False

    @field_validator("received_at", "deadline")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("notification datetimes must include timezone information")
        return value


class DailyDigest(FrozenModel):
    """Private local summary input with no complete message bodies."""

    generated_at: datetime
    priority_items: tuple[DigestItem, ...]
    deadline_items: tuple[DigestItem, ...]
    review_count: int = Field(ge=0)
    pending_action_count: int = Field(ge=0)
    approved_action_count: int = Field(ge=0)

    @field_validator("generated_at")
    @classmethod
    def require_generated_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("summary generation time must include timezone information")
        return value


class NotificationBatchReport(FrozenModel):
    """Bounded result of post-workflow notification processing."""

    priority_alerts: int = Field(default=0, ge=0)
    deadline_alerts: int = Field(default=0, ge=0)
    failure_alerts: int = Field(default=0, ge=0)
    duplicate_events: int = Field(default=0, ge=0)
    summary_path: Path | None = None
    errors: tuple[str, ...] = Field(default=(), max_length=20)
