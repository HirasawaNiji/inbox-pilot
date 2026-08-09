"""Read-only database projections for the Web API."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.sql.selectable import Subquery

from inbox_agent.models import (
    EmailMessage,
    LLMAnalysisResult,
    NormalizedMessage,
    Priority,
    RuleEvaluation,
    TriageResult,
)
from inbox_agent.storage import (
    Database,
    WorkflowRunRecord,
    WorkflowRunRepository,
    storage_counts,
)
from inbox_agent.storage.orm import AnalysisRow, MessageRow
from inbox_agent.web.schemas import (
    HealthResponse,
    MessageDetail,
    MessagePage,
    MessageSummary,
    WorkflowRunResponse,
    aware_datetime,
)


class WebQueryNotFoundError(LookupError):
    """Raised when a requested public projection does not exist."""


@dataclass(frozen=True, slots=True)
class MessageFilters:
    priority: Priority | None = None
    category: str | None = None
    requires_review: bool | None = None
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 200:
            raise ValueError("limit must be between 1 and 200")
        if self.offset < 0:
            raise ValueError("offset must be non-negative")


def _latest_analysis_subquery() -> Subquery:
    return (
        select(
            AnalysisRow.message_id.label("message_id"),
            func.max(AnalysisRow.id).label("analysis_id"),
        )
        .group_by(AnalysisRow.message_id)
        .subquery()
    )


def _summary(message_row: MessageRow, analysis_row: AnalysisRow | None) -> MessageSummary:
    message = EmailMessage.model_validate_json(message_row.payload_json)
    triage = (
        TriageResult.model_validate_json(analysis_row.triage_json)
        if analysis_row is not None
        else None
    )
    return MessageSummary(
        database_id=message_row.id,
        source=message.source,
        source_id=message.source_id,
        subject=message.subject,
        sender_name=message.from_address.name,
        sender_address=message.from_address.address,
        received_at=message.received_at,
        body_preview=message.body_preview,
        has_attachments=message.effective_has_attachments,
        priority=triage.priority if triage is not None else None,
        score=triage.score if triage is not None else None,
        confidence=triage.confidence if triage is not None else None,
        category=triage.category if triage is not None else None,
        decision_source=triage.decision_source if triage is not None else None,
        requires_review=triage.requires_review if triage is not None else None,
        evaluated_at=triage.evaluated_at if triage is not None else None,
    )


class WebQueryService:
    """Expose only stable read projections from the private SQLite database."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def list_messages(self, filters: MessageFilters) -> MessagePage:
        latest = _latest_analysis_subquery()
        base = (
            select(MessageRow, AnalysisRow)
            .outerjoin(latest, latest.c.message_id == MessageRow.id)
            .outerjoin(AnalysisRow, AnalysisRow.id == latest.c.analysis_id)
        )
        if filters.priority is not None:
            base = base.where(AnalysisRow.priority == filters.priority.value)
        if filters.category is not None:
            base = base.where(AnalysisRow.category == filters.category)
        if filters.requires_review is not None:
            base = base.where(AnalysisRow.requires_review == int(filters.requires_review))

        count_statement = select(func.count()).select_from(base.order_by(None).subquery())
        page_statement = (
            base.order_by(MessageRow.received_at.desc(), MessageRow.id.desc())
            .limit(filters.limit)
            .offset(filters.offset)
        )
        with self.database.session() as session:
            total = int(session.scalar(count_statement) or 0)
            rows = session.execute(page_statement).all()
        return MessagePage(
            items=tuple(_summary(message, analysis) for message, analysis in rows),
            total=total,
            limit=filters.limit,
            offset=filters.offset,
        )

    def get_message(self, database_id: int) -> MessageDetail:
        latest = _latest_analysis_subquery()
        statement = (
            select(MessageRow, AnalysisRow)
            .outerjoin(latest, latest.c.message_id == MessageRow.id)
            .outerjoin(AnalysisRow, AnalysisRow.id == latest.c.analysis_id)
            .where(MessageRow.id == database_id)
        )
        with self.database.session() as session:
            row = session.execute(statement).one_or_none()
        if row is None:
            raise WebQueryNotFoundError(f"message does not exist: {database_id}")
        message_row, analysis_row = row
        return MessageDetail(
            database_id=message_row.id,
            message=EmailMessage.model_validate_json(message_row.payload_json),
            normalized=(
                NormalizedMessage.model_validate_json(message_row.normalized_json)
                if message_row.normalized_json is not None
                else None
            ),
            triage=(
                TriageResult.model_validate_json(analysis_row.triage_json)
                if analysis_row is not None
                else None
            ),
            rule_evaluation=(
                RuleEvaluation.model_validate_json(analysis_row.rule_json)
                if analysis_row is not None and analysis_row.rule_json is not None
                else None
            ),
            llm_analysis=(
                LLMAnalysisResult.model_validate_json(analysis_row.llm_json)
                if analysis_row is not None and analysis_row.llm_json is not None
                else None
            ),
        )

    def latest_workflow(self) -> WorkflowRunResponse:
        record = WorkflowRunRepository(self.database).latest()
        if record is None:
            raise WebQueryNotFoundError("no workflow runs are available")
        return self._workflow(record)

    def workflow(self, run_id: str) -> WorkflowRunResponse:
        record = WorkflowRunRepository(self.database).get(run_id)
        if record is None:
            raise WebQueryNotFoundError(f"workflow run does not exist: {run_id}")
        return self._workflow(record)

    @staticmethod
    def _workflow(record: WorkflowRunRecord) -> WorkflowRunResponse:
        started_at = aware_datetime(record.started_at)
        assert started_at is not None
        return WorkflowRunResponse(
            run_id=record.run_id,
            status=record.status,
            current_step=record.current_step,
            started_at=started_at,
            finished_at=aware_datetime(record.finished_at),
            counters=record.counters,
            steps=record.steps,
            error_summary=record.error_summary,
        )

    def health(self, *, revision: str, expected_revision: str) -> HealthResponse:
        ready = revision == expected_revision
        if not ready:
            return HealthResponse(
                status="degraded",
                database_exists=True,
                database_revision=revision,
                expected_revision=expected_revision,
                database_ready=False,
            )
        counts = storage_counts(self.database)
        return HealthResponse(
            status="ok",
            database_exists=True,
            database_revision=revision,
            expected_revision=expected_revision,
            database_ready=ready,
            counts={
                "messages": counts.messages,
                "analyses": counts.analyses,
                "actions": counts.actions,
                "sync_cursors": counts.sync_cursors,
                "workflow_runs": counts.workflow_runs,
            },
        )
