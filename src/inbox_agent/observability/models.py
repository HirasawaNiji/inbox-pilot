"""Validated privacy and telemetry contracts for long-running operation."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator

from inbox_agent.models import FrozenModel, LLMTokenUsage


class EventOutcome(StrEnum):
    """Stable outcomes used by statistics and provider success rates."""

    SUCCEEDED = "succeeded"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"
    SKIPPED = "skipped"


class LLMPricingRate(FrozenModel):
    """User-supplied rates; prices are never hard-coded because providers change them."""

    provider: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    model_name: str = Field(min_length=1, max_length=200)
    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    cached_input_usd_per_million: Decimal | None = Field(default=None, ge=0)

    def estimate_microusd(self, usage: LLMTokenUsage) -> int:
        """Estimate millionths of one USD without binary floating-point drift."""

        cached_rate = self.cached_input_usd_per_million or self.input_usd_per_million
        uncached_tokens = usage.input_tokens - usage.cached_input_tokens
        microusd = (
            self.input_usd_per_million * uncached_tokens
            + cached_rate * usage.cached_input_tokens
            + self.output_usd_per_million * usage.output_tokens
        )
        return int(microusd.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def estimate_llm_cost(
    rates: tuple[LLMPricingRate, ...],
    *,
    provider: str,
    model_name: str,
    usage: LLMTokenUsage | None,
) -> int | None:
    """Return configured cost or None when usage/rates are unavailable."""

    if usage is None:
        return None
    for rate in rates:
        if rate.provider == provider and rate.model_name == model_name:
            return rate.estimate_microusd(usage)
    return None


def safe_message_hash(message_id: str) -> str:
    """Return a stable non-plaintext identifier for local correlation."""

    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()


class ObservabilityEvent(FrozenModel):
    """One event containing metrics but no mail subject, body, or credentials."""

    occurred_at: datetime
    component: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    operation: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    outcome: EventOutcome
    run_id: str | None = Field(default=None, pattern=r"^run-[a-f0-9]{32}$")
    message_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    duration_ms: int | None = Field(default=None, ge=0)
    provider: str | None = Field(default=None, max_length=100)
    model_name: str | None = Field(default=None, max_length=200)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_microusd: int | None = Field(default=None, ge=0)
    error_type: str | None = Field(default=None, max_length=100)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observability timestamps must include timezone information")
        return value


class ObservabilityEventRecord(ObservabilityEvent):
    """Persisted event with its local monotonically increasing identifier."""

    event_id: int = Field(ge=1)


class ProviderStatistics(FrozenModel):
    """Aggregated provider reliability, token, duration, and cost data."""

    provider: str
    model_name: str | None = None
    attempts: int = Field(ge=0)
    successes: int = Field(ge=0)
    failures: int = Field(ge=0)
    success_rate: float | None = Field(default=None, ge=0, le=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    total_duration_ms: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)


class OperationsStatistics(FrozenModel):
    """Privacy-safe operational snapshot used by CLI and future Web views."""

    window_hours: int = Field(ge=1)
    workflow_runs: int = Field(ge=0)
    successful_runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    workflow_success_rate: float | None = Field(default=None, ge=0, le=1)
    average_workflow_duration_ms: int | None = Field(default=None, ge=0)
    review_backlog: int = Field(ge=0)
    action_backlog: int = Field(ge=0)
    notification_backlog: int = Field(ge=0)
    latest_error_type: str | None = None
    latest_error_at: datetime | None = None
    providers: tuple[ProviderStatistics, ...] = ()
