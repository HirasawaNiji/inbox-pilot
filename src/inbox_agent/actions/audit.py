"""Append-only, privacy-bounded audit events for local mailbox actions."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from inbox_agent.actions.dry_run import ActionDryRunPlan, DryRunReport
from inbox_agent.actions.locking import ActionFileLock, ActionFileLockError
from inbox_agent.actions.models import (
    ActionActor,
    MailboxAction,
    MailboxActionStatus,
    MailboxActionType,
)
from inbox_agent.actions.rollback import RollbackDryRunReport
from inbox_agent.models import DecisionSource, FrozenModel, Priority


class ActionAuditEventType(StrEnum):
    """Events currently emitted by the local Stage 3 workflow."""

    ACTION_GENERATED = "action_generated"
    ACTION_STATUS_CHANGED = "action_status_changed"
    DRY_RUN_PLANNED = "dry_run_planned"
    ROLLBACK_DRY_RUN_PLANNED = "rollback_dry_run_planned"
    GRAPH_OPERATION_RECORDED = "graph_operation_recorded"


class AuditGraphOperation(StrEnum):
    """Controlled Graph workflows that may emit an audit outcome."""

    EXECUTE = "execute"
    RECONCILE = "reconcile"


class AuditGraphOutcome(StrEnum):
    """Privacy-safe Graph execution and reconciliation outcomes."""

    ALREADY_SUCCEEDED = "already_succeeded"
    SUCCEEDED = "succeeded"
    NO_CHANGE = "no_change"
    CONFLICT = "conflict"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    READ_FAILED = "read_failed"


class ActionAuditStorageError(Exception):
    """Raised when the private append-only audit log is invalid or unavailable."""


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("audit timestamp must include timezone information")
    return value


def _message_id_hash(message_id: str) -> str:
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()


def _event_id(payload: Mapping[str, object]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]
    return f"audit-{digest}"


class AuditDryRunDetails(FrozenModel):
    """Category counts and safety proof for one dry-run plan."""

    current_category_count: int = Field(ge=0)
    add_categories: tuple[str, ...] = Field(max_length=100)
    remove_categories: tuple[str, ...] = Field(max_length=100)
    final_category_count: int = Field(ge=0)
    would_write: bool
    graph_write_request_count: Literal[0] = 0


class AuditGraphOperationDetails(FrozenModel):
    """Bounded Graph request counts without mailbox content or identifiers."""

    operation: AuditGraphOperation
    outcome: AuditGraphOutcome
    attempt_number: int = Field(ge=0)
    graph_read_request_count: int = Field(ge=0, le=1)
    graph_write_request_count: int = Field(ge=0, le=1)


class ActionAuditEvent(FrozenModel):
    """One strict audit event that excludes raw message IDs and email content."""

    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(pattern=r"^audit-[a-f0-9]{32}$")
    event_type: ActionAuditEventType
    occurred_at: datetime
    actor: ActionActor

    action_id: str = Field(min_length=1, max_length=128)
    message_id_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    action_type: MailboxActionType
    action_status: MailboxActionStatus
    from_status: MailboxActionStatus | None = None
    to_status: MailboxActionStatus | None = None

    priority: Priority
    category: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    requires_review: bool
    decision_source: DecisionSource
    policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=100)

    llm_provider: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=100,
    )
    llm_model: str | None = Field(default=None, min_length=1, max_length=200)
    prompt_version: str | None = Field(default=None, min_length=1, max_length=100)
    provider_request_id: str | None = Field(default=None, min_length=1, max_length=512)

    note: str | None = Field(default=None, min_length=1, max_length=1_000)
    dry_run: AuditDryRunDetails | None = None
    graph_operation: AuditGraphOperationDetails | None = None

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        """Require an absolute event timestamp."""

        return _require_aware(value)

    @model_validator(mode="after")
    def validate_event_shape(self) -> Self:
        """Require event-specific actor, transition, and dry-run fields."""

        if self.event_type is ActionAuditEventType.ACTION_GENERATED:
            if self.actor is not ActionActor.SYSTEM:
                raise ValueError("action_generated requires actor=system")
            if self.action_status is not MailboxActionStatus.PENDING_REVIEW:
                raise ValueError("action_generated must record pending_review")
            if self.from_status is not None or self.to_status is not None:
                raise ValueError("action_generated must not contain a state transition")
            if self.dry_run is not None:
                raise ValueError("action_generated must not contain dry-run details")
            if self.graph_operation is not None:
                raise ValueError("action_generated must not contain Graph operation details")
        elif self.event_type is ActionAuditEventType.ACTION_STATUS_CHANGED:
            if self.from_status is None or self.to_status is None:
                raise ValueError("action_status_changed requires from_status and to_status")
            if self.action_status is not self.to_status:
                raise ValueError("action_status_changed must record the target status")
            if self.dry_run is not None:
                raise ValueError("action_status_changed must not contain dry-run details")
            if self.graph_operation is not None:
                raise ValueError("action_status_changed must not contain Graph operation details")
        elif self.event_type is ActionAuditEventType.DRY_RUN_PLANNED:
            if self.actor is not ActionActor.SYSTEM:
                raise ValueError("dry_run_planned requires actor=system")
            if self.action_status is not MailboxActionStatus.APPROVED:
                raise ValueError("dry_run_planned requires an approved action")
            if self.from_status is not None or self.to_status is not None:
                raise ValueError("dry_run_planned must not change action status")
            if self.dry_run is None:
                raise ValueError("dry_run_planned requires dry-run details")
            if self.graph_operation is not None:
                raise ValueError("dry_run_planned must not contain Graph operation details")
        elif self.event_type is ActionAuditEventType.ROLLBACK_DRY_RUN_PLANNED:
            if self.actor is not ActionActor.USER:
                raise ValueError("rollback_dry_run_planned requires actor=user")
            if self.action_status is not MailboxActionStatus.SUCCEEDED:
                raise ValueError("rollback_dry_run_planned requires a succeeded action")
            if self.from_status is not None or self.to_status is not None:
                raise ValueError("rollback_dry_run_planned must not change action status")
            if self.note is None:
                raise ValueError("rollback_dry_run_planned requires a reason")
            if self.dry_run is None:
                raise ValueError("rollback_dry_run_planned requires dry-run details")
            if self.graph_operation is not None:
                raise ValueError(
                    "rollback_dry_run_planned must not contain Graph operation details"
                )
        elif self.event_type is ActionAuditEventType.GRAPH_OPERATION_RECORDED:
            if self.actor is not ActionActor.SYSTEM:
                raise ValueError("graph_operation_recorded requires actor=system")
            if self.from_status is not None or self.to_status is not None:
                raise ValueError("graph_operation_recorded must not contain a state transition")
            if self.dry_run is not None:
                raise ValueError("graph_operation_recorded must not contain dry-run details")
            if self.graph_operation is None:
                raise ValueError("graph_operation_recorded requires Graph operation details")
            if self.note is None:
                raise ValueError("graph_operation_recorded requires a note")
        return self


class AuditAppendReport(FrozenModel):
    """Counts from one append-only audit write."""

    requested_count: int = Field(ge=0)
    appended_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    log_path: Path


class ActionAuditLog:
    """Validate JSONL events and append new IDs without rewriting history."""

    def __init__(self, path: str | Path, *, lock_timeout_seconds: float = 5.0) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.lock_timeout_seconds = lock_timeout_seconds

    def _lock(self) -> ActionFileLock:
        return ActionFileLock(
            self.lock_path,
            timeout_seconds=self.lock_timeout_seconds,
        )

    def load(self) -> tuple[ActionAuditEvent, ...]:
        """Load and strictly validate every JSONL event."""

        try:
            with self._lock():
                return self._load_unlocked()
        except ActionFileLockError as error:
            raise ActionAuditStorageError(
                f"Unable to lock action audit log: {self.path}"
            ) from error

    def _load_unlocked(self) -> tuple[ActionAuditEvent, ...]:
        """Load audit state while the caller holds the log lock."""

        try:
            raw_content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except UnicodeDecodeError as error:
            raise ActionAuditStorageError(
                f"Action audit log is not valid UTF-8: {self.path}"
            ) from error
        except OSError as error:
            raise ActionAuditStorageError(
                f"Unable to read action audit log: {self.path}"
            ) from error

        events: list[ActionAuditEvent] = []
        event_ids: set[str] = set()
        for line_number, line in enumerate(raw_content.splitlines(), start=1):
            if not line.strip():
                raise ActionAuditStorageError(
                    f"Action audit log contains a blank line at {line_number}: {self.path}"
                )
            try:
                event = ActionAuditEvent.model_validate_json(line)
            except ValueError as error:
                raise ActionAuditStorageError(
                    f"Action audit event is invalid at line {line_number}: {self.path}"
                ) from error
            if event.event_id in event_ids:
                raise ActionAuditStorageError(
                    f"Action audit log contains duplicate event ID at line {line_number}: "
                    f"{self.path}"
                )
            event_ids.add(event.event_id)
            events.append(event)
        return tuple(events)

    def append_unique(self, events: tuple[ActionAuditEvent, ...]) -> AuditAppendReport:
        """Append unseen deterministic event IDs and fsync the JSONL file."""

        try:
            with self._lock():
                existing = self._load_unlocked()
                existing_by_id = {event.event_id: event for event in existing}
                pending: list[ActionAuditEvent] = []
                skipped_count = 0

                for event in events:
                    previous = existing_by_id.get(event.event_id)
                    if previous is None:
                        existing_by_id[event.event_id] = event
                        pending.append(event)
                        continue
                    if previous != event:
                        raise ActionAuditStorageError(
                            "Audit event ID already exists with different content: "
                            f"{event.event_id}"
                        )
                    skipped_count += 1

                if pending:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    lines = (
                        json.dumps(
                            event.model_dump(mode="json"),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        for event in pending
                    )
                    serialized = "".join(f"{line}\n" for line in lines)
                    try:
                        with self.path.open("a", encoding="utf-8", newline="") as handle:
                            handle.write(serialized)
                            handle.flush()
                            os.fsync(handle.fileno())
                    except OSError as error:
                        raise ActionAuditStorageError(
                            f"Unable to append action audit log: {self.path}"
                        ) from error
        except ActionFileLockError as error:
            raise ActionAuditStorageError(
                f"Unable to lock action audit log: {self.path}"
            ) from error

        return AuditAppendReport(
            requested_count=len(events),
            appended_count=len(pending),
            skipped_count=skipped_count,
            total_count=len(existing) + len(pending),
            log_path=self.path,
        )


def _new_event(
    action: MailboxAction,
    *,
    event_id: str,
    event_type: ActionAuditEventType,
    occurred_at: datetime,
    actor: ActionActor,
    action_status: MailboxActionStatus,
    from_status: MailboxActionStatus | None = None,
    to_status: MailboxActionStatus | None = None,
    note: str | None = None,
    dry_run: AuditDryRunDetails | None = None,
    graph_operation: AuditGraphOperationDetails | None = None,
) -> ActionAuditEvent:
    """Construct one event with common privacy-bounded decision metadata."""

    result = action.evidence.triage_result
    llm = action.evidence.llm_analysis
    return ActionAuditEvent(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        actor=actor,
        action_id=action.action_id,
        message_id_sha256=_message_id_hash(action.message_id),
        action_type=action.action_type,
        action_status=action_status,
        from_status=from_status,
        to_status=to_status,
        priority=result.priority,
        category=result.category,
        requires_review=result.requires_review,
        decision_source=result.decision_source,
        policy_version=result.policy_version,
        llm_provider=llm.provider if llm is not None else None,
        llm_model=llm.model_name if llm is not None else None,
        prompt_version=llm.prompt_version if llm is not None else None,
        provider_request_id=llm.request_id if llm is not None else None,
        note=note,
        dry_run=dry_run,
        graph_operation=graph_operation,
    )


def audit_events_for_action(action: MailboxAction) -> tuple[ActionAuditEvent, ...]:
    """Derive deterministic generation and state-transition events from an action."""

    generated_payload = {
        "event_type": ActionAuditEventType.ACTION_GENERATED,
        "action_id": action.action_id,
        "occurred_at": action.created_at.isoformat(),
    }
    events = [
        _new_event(
            action,
            event_id=_event_id(generated_payload),
            event_type=ActionAuditEventType.ACTION_GENERATED,
            occurred_at=action.created_at,
            actor=ActionActor.SYSTEM,
            action_status=MailboxActionStatus.PENDING_REVIEW,
        )
    ]
    for transition in action.transition_history:
        transition_payload = {
            "event_type": ActionAuditEventType.ACTION_STATUS_CHANGED,
            "action_id": action.action_id,
            "occurred_at": transition.occurred_at.isoformat(),
            "from_status": transition.from_status,
            "to_status": transition.to_status,
        }
        events.append(
            _new_event(
                action,
                event_id=_event_id(transition_payload),
                event_type=ActionAuditEventType.ACTION_STATUS_CHANGED,
                occurred_at=transition.occurred_at,
                actor=transition.actor,
                action_status=transition.to_status,
                from_status=transition.from_status,
                to_status=transition.to_status,
                note=transition.note,
            )
        )
    return tuple(events)


def audit_event_for_graph_operation(
    action: MailboxAction,
    *,
    occurred_at: datetime,
    operation: AuditGraphOperation,
    outcome: AuditGraphOutcome,
    attempt_number: int,
    graph_read_request_count: int,
    graph_write_request_count: int,
    note: str,
) -> ActionAuditEvent:
    """Build one deterministic, content-free Graph operation event."""

    payload = {
        "event_type": ActionAuditEventType.GRAPH_OPERATION_RECORDED,
        "action_id": action.action_id,
        "occurred_at": occurred_at.isoformat(),
        "operation": operation,
        "outcome": outcome,
        "attempt_number": attempt_number,
        "graph_read_request_count": graph_read_request_count,
        "graph_write_request_count": graph_write_request_count,
    }
    return _new_event(
        action,
        event_id=_event_id(payload),
        event_type=ActionAuditEventType.GRAPH_OPERATION_RECORDED,
        occurred_at=occurred_at,
        actor=ActionActor.SYSTEM,
        action_status=action.status,
        note=note,
        graph_operation=AuditGraphOperationDetails(
            operation=operation,
            outcome=outcome,
            attempt_number=attempt_number,
            graph_read_request_count=graph_read_request_count,
            graph_write_request_count=graph_write_request_count,
        ),
    )


def _dry_run_event(
    action: MailboxAction,
    plan: ActionDryRunPlan,
    generated_at: datetime,
) -> ActionAuditEvent:
    payload = {
        "event_type": ActionAuditEventType.DRY_RUN_PLANNED,
        "action_id": action.action_id,
        "occurred_at": generated_at.isoformat(),
        "add_categories": plan.add_categories,
        "remove_categories": plan.remove_categories,
    }
    return _new_event(
        action,
        event_id=_event_id(payload),
        event_type=ActionAuditEventType.DRY_RUN_PLANNED,
        occurred_at=generated_at,
        actor=ActionActor.SYSTEM,
        action_status=MailboxActionStatus.APPROVED,
        dry_run=AuditDryRunDetails(
            current_category_count=len(plan.current_categories),
            add_categories=plan.add_categories,
            remove_categories=plan.remove_categories,
            final_category_count=len(plan.final_categories),
            would_write=plan.would_write,
        ),
    )


def audit_events_for_dry_run(
    actions: tuple[MailboxAction, ...],
    report: DryRunReport,
) -> tuple[ActionAuditEvent, ...]:
    """Build one privacy-bounded event for each dry-run plan."""

    actions_by_id = {action.action_id: action for action in actions}
    events: list[ActionAuditEvent] = []
    for plan in report.plans:
        action = actions_by_id.get(plan.action_id)
        if action is None:
            raise ValueError(f"Dry-run plan has no matching action: {plan.action_id}")
        events.append(_dry_run_event(action, plan, report.generated_at))
    return tuple(events)


def audit_event_for_rollback_dry_run(
    action: MailboxAction,
    report: RollbackDryRunReport,
) -> ActionAuditEvent:
    """Build one privacy-bounded event for an explicit rollback preview."""

    plan = report.plan
    if action.action_id != plan.action_id:
        raise ValueError(f"Rollback plan has no matching action: {plan.action_id}")
    payload = {
        "event_type": ActionAuditEventType.ROLLBACK_DRY_RUN_PLANNED,
        "action_id": action.action_id,
        "occurred_at": report.generated_at.isoformat(),
        "rollback_idempotency_key": plan.rollback_idempotency_key,
        "add_categories": plan.add_categories,
        "remove_categories": plan.remove_categories,
    }
    return _new_event(
        action,
        event_id=_event_id(payload),
        event_type=ActionAuditEventType.ROLLBACK_DRY_RUN_PLANNED,
        occurred_at=report.generated_at,
        actor=ActionActor.USER,
        action_status=MailboxActionStatus.SUCCEEDED,
        note=plan.reason,
        dry_run=AuditDryRunDetails(
            current_category_count=len(plan.expected_current_categories),
            add_categories=plan.add_categories,
            remove_categories=plan.remove_categories,
            final_category_count=len(plan.final_categories),
            would_write=plan.would_write,
        ),
    )
