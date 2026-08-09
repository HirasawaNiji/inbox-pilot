"""Typed repositories that preserve InboxPilot's Pydantic contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from inbox_agent.actions.models import MailboxAction
from inbox_agent.models import (
    EmailMessage,
    LLMAnalysisResult,
    MailSource,
    NormalizedMessage,
    RuleEvaluation,
    TriageResult,
)
from inbox_agent.storage.database import Database
from inbox_agent.storage.orm import (
    ActionRow,
    AnalysisRow,
    MessageRow,
    ServiceStateRow,
    SyncCursorRow,
    WorkflowRunRow,
)


class StorageError(RuntimeError):
    """Raised when a storage contract cannot be satisfied."""


class UpsertOutcome(StrEnum):
    """Result of writing a provider message or mutable snapshot."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """Stable outcome returned by repository upserts."""

    outcome: UpsertOutcome
    row_id: int | str


@dataclass(frozen=True, slots=True)
class StorageCounts:
    """Small database status summary for the CLI."""

    messages: int
    analyses: int
    actions: int
    sync_cursors: int
    workflow_runs: int


@dataclass(frozen=True, slots=True)
class WorkflowRunRecord:
    """Primitive durable workflow state used by status commands and recovery."""

    run_id: str
    status: str
    current_step: str | None
    started_at: str
    finished_at: str | None
    counters: dict[str, int]
    steps: tuple[dict[str, object], ...]
    error_summary: str | None


@dataclass(frozen=True, slots=True)
class ServiceStateRecord:
    """Latest durable state for one local scheduler instance name."""

    service_name: str
    status: str
    pid: int | None
    started_at: str | None
    last_run_at: str | None
    last_success_at: str | None
    last_failure_at: str | None
    next_run_at: str | None
    last_run_id: str | None
    consecutive_failures: int
    last_error: str | None
    updated_at: str


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _message_row(session: Session, source: MailSource, source_id: str) -> MessageRow:
    row = session.scalar(
        select(MessageRow).where(
            MessageRow.source == source.value,
            MessageRow.source_id == source_id,
        )
    )
    if row is None:
        raise StorageError(f"message not found: {source.value}/{source_id}")
    return row


class MessageRepository:
    """Store and restore provider-neutral email messages idempotently."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def upsert(self, message: EmailMessage) -> UpsertResult:
        payload = _canonical_json(message)
        content_hash = _sha256(payload)
        now = _now_text()
        with self._database.session() as session:
            row = session.scalar(
                select(MessageRow).where(
                    MessageRow.source == message.source.value,
                    MessageRow.source_id == message.source_id,
                )
            )
            if row is None:
                row = MessageRow(
                    source=message.source.value,
                    source_id=message.source_id,
                    internet_message_id=message.internet_message_id,
                    subject=message.subject,
                    from_address=message.from_address.address,
                    received_at=message.received_at.isoformat(),
                    change_key=message.change_key,
                    content_hash=content_hash,
                    payload_json=payload,
                    normalized_json=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                return UpsertResult(UpsertOutcome.CREATED, row.id)
            if row.content_hash == content_hash:
                return UpsertResult(UpsertOutcome.UNCHANGED, row.id)

            row.internet_message_id = message.internet_message_id
            row.subject = message.subject
            row.from_address = message.from_address.address
            row.received_at = message.received_at.isoformat()
            row.change_key = message.change_key
            row.content_hash = content_hash
            row.payload_json = payload
            row.updated_at = now
            return UpsertResult(UpsertOutcome.UPDATED, row.id)

    def get(self, source: MailSource, source_id: str) -> EmailMessage | None:
        with self._database.session() as session:
            row = session.scalar(
                select(MessageRow).where(
                    MessageRow.source == source.value,
                    MessageRow.source_id == source_id,
                )
            )
            return EmailMessage.model_validate_json(row.payload_json) if row is not None else None

    def list(self, *, limit: int = 100, offset: int = 0) -> tuple[EmailMessage, ...]:
        if limit < 1 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        with self._database.session() as session:
            rows = session.scalars(
                select(MessageRow)
                .order_by(MessageRow.received_at.desc(), MessageRow.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
            return tuple(EmailMessage.model_validate_json(row.payload_json) for row in rows)

    def save_normalized(self, message: NormalizedMessage) -> UpsertOutcome:
        """Attach the deterministic cleaned representation to an imported message."""

        payload = _canonical_json(message)
        with self._database.session() as session:
            row = _message_row(session, message.source, message.source_id)
            if row.normalized_json == payload:
                return UpsertOutcome.UNCHANGED
            row.normalized_json = payload
            row.updated_at = _now_text()
            return UpsertOutcome.UPDATED

    def get_normalized(self, source: MailSource, source_id: str) -> NormalizedMessage | None:
        """Restore a stored normalized message when one has been computed."""

        with self._database.session() as session:
            row = session.scalar(
                select(MessageRow).where(
                    MessageRow.source == source.value,
                    MessageRow.source_id == source_id,
                )
            )
            if row is None or row.normalized_json is None:
                return None
            return NormalizedMessage.model_validate_json(row.normalized_json)

    def content_hash(self, source: MailSource, source_id: str) -> str:
        """Return the canonical raw-message hash used by workflow idempotency."""

        with self._database.session() as session:
            return _message_row(session, source, source_id).content_hash


class AnalysisRepository:
    """Persist one retryable analysis slot per message content and profile."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def save(
        self,
        *,
        source: MailSource,
        result: TriageResult,
        rule_evaluation: RuleEvaluation | None = None,
        llm_analysis: LLMAnalysisResult | None = None,
        analysis_profile: str = "0" * 64,
        complete: bool = True,
    ) -> UpsertResult:
        if len(analysis_profile) != 64 or any(
            character not in "0123456789abcdef" for character in analysis_profile
        ):
            raise ValueError("analysis_profile must be a lowercase SHA-256 digest")
        triage_json = _canonical_json(result)
        rule_json = _canonical_json(rule_evaluation) if rule_evaluation is not None else None
        llm_json = _canonical_json(llm_analysis) if llm_analysis is not None else None
        fingerprint = _sha256(_canonical_json([triage_json, rule_json, llm_json]))
        with self._database.session() as session:
            message = _message_row(session, source, result.message_id)
            existing = session.scalar(
                select(AnalysisRow).where(
                    AnalysisRow.message_id == message.id,
                    AnalysisRow.message_content_hash == message.content_hash,
                    AnalysisRow.analysis_profile == analysis_profile,
                )
            )
            if existing is not None:
                if existing.fingerprint == fingerprint and existing.complete == int(complete):
                    return UpsertResult(UpsertOutcome.UNCHANGED, existing.id)
                existing.fingerprint = fingerprint
                existing.complete = int(complete)
                existing.priority = result.priority.value
                existing.category = result.category
                existing.decision_source = result.decision_source.value
                existing.requires_review = int(result.requires_review)
                existing.policy_version = result.policy_version
                existing.evaluated_at = result.evaluated_at.isoformat()
                existing.triage_json = triage_json
                existing.rule_json = rule_json
                existing.llm_json = llm_json
                existing.created_at = _now_text()
                return UpsertResult(UpsertOutcome.UPDATED, existing.id)

            row = AnalysisRow(
                message_id=message.id,
                fingerprint=fingerprint,
                message_content_hash=message.content_hash,
                analysis_profile=analysis_profile,
                complete=int(complete),
                priority=result.priority.value,
                category=result.category,
                decision_source=result.decision_source.value,
                requires_review=int(result.requires_review),
                policy_version=result.policy_version,
                evaluated_at=result.evaluated_at.isoformat(),
                triage_json=triage_json,
                rule_json=rule_json,
                llm_json=llm_json,
                created_at=_now_text(),
            )
            session.add(row)
            session.flush()
            return UpsertResult(UpsertOutcome.CREATED, row.id)

    def has_current(
        self,
        source: MailSource,
        source_id: str,
        analysis_profile: str,
    ) -> bool:
        """Return whether the current message content has a complete matching analysis."""

        with self._database.session() as session:
            message = session.scalar(
                select(MessageRow).where(
                    MessageRow.source == source.value,
                    MessageRow.source_id == source_id,
                )
            )
            if message is None:
                return False
            row_id = session.scalar(
                select(AnalysisRow.id).where(
                    AnalysisRow.message_id == message.id,
                    AnalysisRow.message_content_hash == message.content_hash,
                    AnalysisRow.analysis_profile == analysis_profile,
                    AnalysisRow.complete == 1,
                )
            )
            return row_id is not None

    def latest(self, source: MailSource, source_id: str) -> TriageResult | None:
        with self._database.session() as session:
            message = session.scalar(
                select(MessageRow).where(
                    MessageRow.source == source.value,
                    MessageRow.source_id == source_id,
                )
            )
            if message is None:
                return None
            row = session.scalar(
                select(AnalysisRow)
                .where(AnalysisRow.message_id == message.id)
                .order_by(AnalysisRow.evaluated_at.desc(), AnalysisRow.id.desc())
                .limit(1)
            )
            return TriageResult.model_validate_json(row.triage_json) if row is not None else None


class MailboxActionRepository:
    """Persist the latest immutable action snapshot by stable action ID."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def upsert(self, *, source: MailSource, action: MailboxAction) -> UpsertResult:
        payload = _canonical_json(action)
        payload_hash = _sha256(payload)
        with self._database.session() as session:
            message = _message_row(session, source, action.message_id)
            row = session.get(ActionRow, action.action_id)
            if row is None:
                row = ActionRow(
                    action_id=action.action_id,
                    message_id=message.id,
                    status=action.status.value,
                    idempotency_key=action.idempotency_key,
                    payload_hash=payload_hash,
                    payload_json=payload,
                    created_at=action.created_at.isoformat(),
                    updated_at=action.updated_at.isoformat(),
                )
                session.add(row)
                return UpsertResult(UpsertOutcome.CREATED, action.action_id)
            if row.message_id != message.id:
                raise StorageError("action ID is already associated with another message")
            if row.payload_hash == payload_hash:
                return UpsertResult(UpsertOutcome.UNCHANGED, action.action_id)

            row.status = action.status.value
            row.idempotency_key = action.idempotency_key
            row.payload_hash = payload_hash
            row.payload_json = payload
            row.updated_at = action.updated_at.isoformat()
            return UpsertResult(UpsertOutcome.UPDATED, action.action_id)

    def get(self, action_id: str) -> MailboxAction | None:
        with self._database.session() as session:
            row = session.get(ActionRow, action_id)
            return MailboxAction.model_validate_json(row.payload_json) if row is not None else None


class SyncCursorRepository:
    """Persist opaque incremental-sync state without exposing it to Git."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def set(
        self,
        *,
        provider: str,
        mailbox_key: str,
        folder_key: str,
        cursor: dict[str, Any],
    ) -> UpsertOutcome:
        payload = _canonical_json(cursor)
        with self._database.session() as session:
            row = session.scalar(
                select(SyncCursorRow).where(
                    SyncCursorRow.provider == provider,
                    SyncCursorRow.mailbox_key == mailbox_key,
                    SyncCursorRow.folder_key == folder_key,
                )
            )
            if row is None:
                session.add(
                    SyncCursorRow(
                        provider=provider,
                        mailbox_key=mailbox_key,
                        folder_key=folder_key,
                        cursor_json=payload,
                        updated_at=_now_text(),
                    )
                )
                return UpsertOutcome.CREATED
            if row.cursor_json == payload:
                return UpsertOutcome.UNCHANGED
            row.cursor_json = payload
            row.updated_at = _now_text()
            return UpsertOutcome.UPDATED

    def get(self, *, provider: str, mailbox_key: str, folder_key: str) -> dict[str, Any] | None:
        with self._database.session() as session:
            row = session.scalar(
                select(SyncCursorRow).where(
                    SyncCursorRow.provider == provider,
                    SyncCursorRow.mailbox_key == mailbox_key,
                    SyncCursorRow.folder_key == folder_key,
                )
            )
            if row is None:
                return None
            value = json.loads(row.cursor_json)
            if not isinstance(value, dict):
                raise StorageError("stored sync cursor is not a JSON object")
            return value


class WorkflowRunRepository:
    """Persist resumable run progress and return recent status summaries."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def save(
        self,
        *,
        run_id: str,
        status: str,
        current_step: str | None,
        started_at: str,
        finished_at: str | None,
        counters: Mapping[str, int],
        steps: tuple[dict[str, object], ...],
        error_summary: str | None = None,
    ) -> None:
        counters_json = _canonical_json(dict(counters))
        steps_json = _canonical_json(steps)
        with self._database.session() as session:
            row = session.get(WorkflowRunRow, run_id)
            if row is None:
                session.add(
                    WorkflowRunRow(
                        run_id=run_id,
                        status=status,
                        current_step=current_step,
                        started_at=started_at,
                        finished_at=finished_at,
                        counters_json=counters_json,
                        steps_json=steps_json,
                        error_summary=error_summary,
                    )
                )
                return
            row.status = status
            row.current_step = current_step
            row.finished_at = finished_at
            row.counters_json = counters_json
            row.steps_json = steps_json
            row.error_summary = error_summary

    def latest(self) -> WorkflowRunRecord | None:
        with self._database.session() as session:
            row = session.scalar(
                select(WorkflowRunRow)
                .order_by(WorkflowRunRow.started_at.desc(), WorkflowRunRow.run_id.desc())
                .limit(1)
            )
            return _workflow_record(row) if row is not None else None

    def get(self, run_id: str) -> WorkflowRunRecord | None:
        with self._database.session() as session:
            row = session.get(WorkflowRunRow, run_id)
            return _workflow_record(row) if row is not None else None


def _workflow_record(row: WorkflowRunRow) -> WorkflowRunRecord:
    counters_value = json.loads(row.counters_json)
    steps_value = json.loads(row.steps_json)
    if not isinstance(counters_value, dict) or not all(
        isinstance(key, str) and isinstance(value, int) for key, value in counters_value.items()
    ):
        raise StorageError("stored workflow counters are invalid")
    if not isinstance(steps_value, list) or not all(isinstance(step, dict) for step in steps_value):
        raise StorageError("stored workflow steps are invalid")
    return WorkflowRunRecord(
        run_id=row.run_id,
        status=row.status,
        current_step=row.current_step,
        started_at=row.started_at,
        finished_at=row.finished_at,
        counters=dict(counters_value),
        steps=tuple(dict(step) for step in steps_value),
        error_summary=row.error_summary,
    )


class ServiceStateRepository:
    """Store one replaceable status row for a named local scheduler."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def save(self, state: ServiceStateRecord) -> None:
        with self._database.session() as session:
            row = session.get(ServiceStateRow, state.service_name)
            if row is None:
                session.add(
                    ServiceStateRow(
                        service_name=state.service_name,
                        status=state.status,
                        pid=state.pid,
                        started_at=state.started_at,
                        last_run_at=state.last_run_at,
                        last_success_at=state.last_success_at,
                        last_failure_at=state.last_failure_at,
                        next_run_at=state.next_run_at,
                        last_run_id=state.last_run_id,
                        consecutive_failures=state.consecutive_failures,
                        last_error=state.last_error,
                        updated_at=state.updated_at,
                    )
                )
                return
            row.status = state.status
            row.pid = state.pid
            row.started_at = state.started_at
            row.last_run_at = state.last_run_at
            row.last_success_at = state.last_success_at
            row.last_failure_at = state.last_failure_at
            row.next_run_at = state.next_run_at
            row.last_run_id = state.last_run_id
            row.consecutive_failures = state.consecutive_failures
            row.last_error = state.last_error
            row.updated_at = state.updated_at

    def get(self, service_name: str) -> ServiceStateRecord | None:
        with self._database.session() as session:
            row = session.get(ServiceStateRow, service_name)
            if row is None:
                return None
            return ServiceStateRecord(
                service_name=row.service_name,
                status=row.status,
                pid=row.pid,
                started_at=row.started_at,
                last_run_at=row.last_run_at,
                last_success_at=row.last_success_at,
                last_failure_at=row.last_failure_at,
                next_run_at=row.next_run_at,
                last_run_id=row.last_run_id,
                consecutive_failures=row.consecutive_failures,
                last_error=row.last_error,
                updated_at=row.updated_at,
            )


def storage_counts(database: Database) -> StorageCounts:
    """Count durable records without loading private payloads."""

    with database.session() as session:
        return StorageCounts(
            messages=session.scalar(select(func.count()).select_from(MessageRow)) or 0,
            analyses=session.scalar(select(func.count()).select_from(AnalysisRow)) or 0,
            actions=session.scalar(select(func.count()).select_from(ActionRow)) or 0,
            sync_cursors=session.scalar(select(func.count()).select_from(SyncCursorRow)) or 0,
            workflow_runs=session.scalar(select(func.count()).select_from(WorkflowRunRow)) or 0,
        )
