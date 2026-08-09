"""Public request and response contracts for the local Web API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from inbox_agent.actions import (
    ActionGraphExecutionReport,
    ActionReconciliationReport,
    DryRunReport,
    MailboxAction,
    RollbackDryRunReport,
    RollbackExecutionReport,
    RollbackReconciliationReport,
)
from inbox_agent.models import (
    DecisionSource,
    EmailMessage,
    LLMAnalysisResult,
    MailSource,
    NormalizedMessage,
    Priority,
    RuleEvaluation,
    TriageResult,
)


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ErrorBody(APIModel):
    code: str
    message: str
    request_id: str | None = None


class ErrorResponse(APIModel):
    error: ErrorBody


class MessageSummary(APIModel):
    database_id: int
    source: MailSource
    source_id: str
    subject: str
    sender_name: str
    sender_address: str
    received_at: datetime
    body_preview: str
    has_attachments: bool
    priority: Priority | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    category: str | None = None
    decision_source: DecisionSource | None = None
    requires_review: bool | None = None
    evaluated_at: datetime | None = None


class MessagePage(APIModel):
    items: tuple[MessageSummary, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)


class MessageDetail(APIModel):
    database_id: int
    message: EmailMessage
    normalized: NormalizedMessage | None = None
    triage: TriageResult | None = None
    rule_evaluation: RuleEvaluation | None = None
    llm_analysis: LLMAnalysisResult | None = None


class ActionPage(APIModel):
    items: tuple[MailboxAction, ...]
    total: int = Field(ge=0)


class ReviewRequest(APIModel):
    note: str | None = Field(default=None, min_length=1, max_length=1_000)


class RejectRequest(APIModel):
    reason: str | None = Field(default=None, min_length=1, max_length=1_000)


class ExecuteRequest(APIModel):
    confirm_action_id: str = Field(min_length=8, max_length=128)
    idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReconcileRequest(APIModel):
    idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class RollbackPreviewRequest(APIModel):
    reason: str = Field(min_length=1, max_length=1_000)


class RollbackExecuteRequest(APIModel):
    reason: str = Field(min_length=1, max_length=1_000)
    confirm_action_id: str = Field(min_length=8, max_length=128)
    rollback_idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class RollbackReconcileRequest(APIModel):
    rollback_idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkflowRunResponse(APIModel):
    run_id: str
    status: str
    current_step: str | None
    started_at: datetime
    finished_at: datetime | None
    counters: dict[str, int]
    steps: tuple[dict[str, object], ...]
    error_summary: str | None


class HealthResponse(APIModel):
    status: str
    database_exists: bool
    database_revision: str | None = None
    expected_revision: str
    database_ready: bool
    counts: dict[str, int] | None = None


ActionPreviewResponse = DryRunReport
ActionExecuteResponse = ActionGraphExecutionReport
ActionReconcileResponse = ActionReconciliationReport
RollbackPreviewResponse = RollbackDryRunReport
RollbackExecuteResponse = RollbackExecutionReport
RollbackReconcileResponse = RollbackReconciliationReport


def aware_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored datetime must include timezone information")
    return parsed
