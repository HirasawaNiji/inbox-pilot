"""SQLAlchemy tables for InboxPilot's private local state."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base shared by the initial Stage 4 schema."""


class MessageRow(Base):
    """Provider-neutral message plus queryable identity fields."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_messages_source_identity"),
        Index("ix_messages_received_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    internet_message_id: Mapped[str | None] = mapped_column(String(998))
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    received_at: Mapped[str] = mapped_column(String(40), nullable=False)
    change_key: Mapped[str | None] = mapped_column(String(512))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    analyses: Mapped[list[AnalysisRow]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
    actions: Mapped[list[ActionRow]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class AnalysisRow(Base):
    """Immutable analysis snapshot with indexed final decision fields."""

    __tablename__ = "analyses"
    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "message_content_hash",
            "analysis_profile",
            name="uq_analyses_current_profile",
        ),
        Index("ix_analyses_priority_evaluated", "priority", "evaluated_at"),
        Index("ix_analyses_review_evaluated", "requires_review", "evaluated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    message_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    complete: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[str] = mapped_column(String(2), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    decision_source: Mapped[str] = mapped_column(String(16), nullable=False)
    requires_review: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    evaluated_at: Mapped[str] = mapped_column(String(40), nullable=False)
    triage_json: Mapped[str] = mapped_column(Text, nullable=False)
    rule_json: Mapped[str | None] = mapped_column(Text)
    llm_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)

    message: Mapped[MessageRow] = relationship(back_populates="analyses")


class ActionRow(Base):
    """Latest validated mailbox-action snapshot."""

    __tablename__ = "mailbox_actions"
    __table_args__ = (Index("ix_mailbox_actions_status_updated", "status", "updated_at"),)

    action_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)

    message: Mapped[MessageRow] = relationship(back_populates="actions")


class SyncCursorRow(Base):
    """Opaque provider cursor; its payload remains inside the private database."""

    __tablename__ = "sync_cursors"
    __table_args__ = (
        UniqueConstraint("provider", "mailbox_key", "folder_key", name="uq_sync_cursor_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    mailbox_key: Mapped[str] = mapped_column(String(320), nullable=False)
    folder_key: Mapped[str] = mapped_column(String(256), nullable=False)
    cursor_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class WorkflowRunRow(Base):
    """One durable workflow-run summary for later scheduling and observability."""

    __tablename__ = "workflow_runs"
    __table_args__ = (Index("ix_workflow_runs_status_started", "status", "started_at"),)

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[str] = mapped_column(String(40), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String(40))
    counters_json: Mapped[str] = mapped_column(Text, nullable=False)
    steps_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)


class ServiceStateRow(Base):
    """Latest scheduler state for one named local service."""

    __tablename__ = "service_states"

    service_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[str | None] = mapped_column(String(40))
    last_run_at: Mapped[str | None] = mapped_column(String(40))
    last_success_at: Mapped[str | None] = mapped_column(String(40))
    last_failure_at: Mapped[str | None] = mapped_column(String(40))
    next_run_at: Mapped[str | None] = mapped_column(String(40))
    last_run_id: Mapped[str | None] = mapped_column(String(64))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(40), nullable=False)


class NotificationDeliveryRow(Base):
    """Privacy-safe delivery ledger used for durable notification deduplication."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (Index("ix_notification_deliveries_status_kind", "status", "kind"),)

    dedupe_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    related_hash: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    last_attempt_at: Mapped[str] = mapped_column(String(40), nullable=False)
    delivered_at: Mapped[str | None] = mapped_column(String(40))
    error_summary: Mapped[str | None] = mapped_column(Text)


class ObservabilityEventRow(Base):
    """Privacy-bounded event used for run, provider, and message tracing."""

    __tablename__ = "observability_events"
    __table_args__ = (
        Index("ix_observability_events_run_time", "run_id", "occurred_at"),
        Index("ix_observability_events_message_time", "message_hash", "occurred_at"),
        Index("ix_observability_events_provider_outcome", "provider", "outcome"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64))
    message_hash: Mapped[str | None] = mapped_column(String(64))
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str | None] = mapped_column(String(200))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_microusd: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(100))
    details_json: Mapped[str] = mapped_column(Text, nullable=False)
