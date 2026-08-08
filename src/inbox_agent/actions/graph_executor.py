"""Conflict-safe execution of one approved Outlook category action."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from inbox_agent.actions.audit import (
    ActionAuditEvent,
    ActionAuditStorageError,
    AuditAppendReport,
    AuditGraphOperation,
    AuditGraphOutcome,
    audit_event_for_graph_operation,
    audit_events_for_action,
)
from inbox_agent.actions.models import (
    MANAGED_CATEGORY_PREFIX,
    MailboxAction,
    MailboxActionStatus,
)
from inbox_agent.actions.queue import ActionQueueRepository, ActionQueueStorageError
from inbox_agent.graph import (
    GraphAccessToken,
    GraphCategoryWriteRequest,
    GraphCategoryWriteResult,
    GraphMessageCategorySnapshot,
    GraphRequestError,
    GraphWriteOutcomeUnknownError,
)
from inbox_agent.models import FrozenModel


class CategoryGraphClientProtocol(Protocol):
    """Graph operations required by the controlled executor."""

    def get_category_snapshot(
        self,
        message_id: str,
        token: GraphAccessToken,
    ) -> GraphMessageCategorySnapshot: ...

    def set_categories(
        self,
        request: GraphCategoryWriteRequest,
        token: GraphAccessToken,
    ) -> GraphCategoryWriteResult: ...


class ActionAuditLogProtocol(Protocol):
    """Append-only audit operation required by execution and reconciliation."""

    def append_unique(self, events: tuple[ActionAuditEvent, ...]) -> AuditAppendReport: ...


class ActionGraphExecutionOutcome(StrEnum):
    """Auditable result classes for one controlled execution attempt."""

    ALREADY_SUCCEEDED = "already_succeeded"
    SUCCEEDED = "succeeded"
    NO_CHANGE = "no_change"
    CONFLICT = "conflict"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ActionGraphExecutionReport(FrozenModel):
    """Bounded result without raw categories, message IDs, or token data."""

    action_id: str = Field(min_length=1, max_length=128)
    outcome: ActionGraphExecutionOutcome
    final_status: MailboxActionStatus
    attempt_number: int = Field(ge=0)
    graph_read_request_count: int = Field(ge=0, le=1)
    graph_write_request_count: int = Field(ge=0, le=1)
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        expected = {
            ActionGraphExecutionOutcome.ALREADY_SUCCEEDED: (
                MailboxActionStatus.SUCCEEDED,
                0,
                0,
            ),
            ActionGraphExecutionOutcome.SUCCEEDED: (MailboxActionStatus.SUCCEEDED, 1, 1),
            ActionGraphExecutionOutcome.NO_CHANGE: (MailboxActionStatus.SUCCEEDED, 1, 0),
            ActionGraphExecutionOutcome.CONFLICT: (MailboxActionStatus.FAILED, 1, 0),
            ActionGraphExecutionOutcome.OUTCOME_UNKNOWN: (
                MailboxActionStatus.OUTCOME_UNKNOWN,
                1,
                1,
            ),
        }
        fixed = expected.get(self.outcome)
        if fixed is not None:
            status, read_count, write_count = fixed
            if (
                self.final_status is not status
                or self.graph_read_request_count != read_count
                or self.graph_write_request_count != write_count
            ):
                raise ValueError("execution report counts do not match the outcome")
        elif self.outcome is ActionGraphExecutionOutcome.FAILED:
            if self.final_status is not MailboxActionStatus.FAILED:
                raise ValueError("failed execution report must have failed status")
            if self.graph_read_request_count != 1:
                raise ValueError("failed execution must include the preflight read attempt")

        if (
            self.outcome
            in {
                ActionGraphExecutionOutcome.CONFLICT,
                ActionGraphExecutionOutcome.FAILED,
                ActionGraphExecutionOutcome.OUTCOME_UNKNOWN,
            }
            and self.reason is None
        ):
            raise ValueError("non-success execution report requires a reason")
        return self


class ActionExecutionPersistenceError(Exception):
    """Raised when mailbox outcome and private queue state may differ."""


class ActionExecutionAuditError(Exception):
    """Raised when a queue transition cannot be durably audited."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _category_keys(categories: tuple[str, ...]) -> frozenset[str]:
    return frozenset(category.casefold() for category in categories)


def _final_categories(action: MailboxAction) -> tuple[str, ...]:
    unmanaged = tuple(
        category
        for category in action.current_snapshot.categories
        if not category.casefold().startswith(MANAGED_CATEGORY_PREFIX.casefold())
    )
    return (*unmanaged, *action.write_plan.managed_categories)


def _safe_reason(prefix: str, error: Exception | None = None) -> str:
    if error is None:
        return prefix[:500]
    detail = str(error).replace("\r", " ").replace("\n", " ")
    return f"{prefix}: {type(error).__name__}: {detail}"[:500]


def _attempt_number(action: MailboxAction) -> int:
    return sum(
        transition.to_status is MailboxActionStatus.EXECUTING
        for transition in action.transition_history
    )


class ApprovedActionGraphExecutor:
    """Claim, preflight, and execute one human-approved category action."""

    def __init__(
        self,
        repository: ActionQueueRepository,
        graph_client: CategoryGraphClientProtocol,
        audit_log: ActionAuditLogProtocol,
    ) -> None:
        self.repository = repository
        self.graph_client = graph_client
        self.audit_log = audit_log

    def _audit(self, action: MailboxAction, phase: str) -> None:
        try:
            self.audit_log.append_unique(audit_events_for_action(action))
        except ActionAuditStorageError as error:
            raise ActionExecutionAuditError(
                f"Unable to persist action audit after {phase}: {action.action_id}"
            ) from error

    def _audit_outcome(
        self,
        action: MailboxAction,
        phase: str,
        *,
        outcome: ActionGraphExecutionOutcome,
        attempt_number: int,
        graph_read_request_count: int,
        graph_write_request_count: int,
        note: str,
    ) -> None:
        operation_event = audit_event_for_graph_operation(
            action,
            occurred_at=action.updated_at,
            operation=AuditGraphOperation.EXECUTE,
            outcome=AuditGraphOutcome(outcome.value),
            attempt_number=attempt_number,
            graph_read_request_count=graph_read_request_count,
            graph_write_request_count=graph_write_request_count,
            note=note,
        )
        try:
            self.audit_log.append_unique((*audit_events_for_action(action), operation_event))
        except ActionAuditStorageError as error:
            raise ActionExecutionAuditError(
                f"Unable to persist action audit after {phase}: {action.action_id}"
            ) from error

    def execute(
        self,
        action_id: str,
        idempotency_key: str,
        token: GraphAccessToken,
    ) -> ActionGraphExecutionReport:
        """Execute at most one PATCH after an exact live snapshot comparison."""

        claim = self.repository.claim_execution(action_id, idempotency_key)
        if not claim.should_execute:
            self._audit_outcome(
                claim.action,
                "already-succeeded no-op",
                outcome=ActionGraphExecutionOutcome.ALREADY_SUCCEEDED,
                attempt_number=claim.attempt_number,
                graph_read_request_count=0,
                graph_write_request_count=0,
                note="Execution skipped because the action already succeeded",
            )
            return ActionGraphExecutionReport(
                action_id=claim.action.action_id,
                outcome=ActionGraphExecutionOutcome.ALREADY_SUCCEEDED,
                final_status=claim.action.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=0,
                graph_write_request_count=0,
            )

        action = claim.action
        self._audit(action, "execution claim")
        try:
            live = self.graph_client.get_category_snapshot(action.message_id, token)
        except GraphRequestError as error:
            reason = _safe_reason("Graph preflight read failed", error)
            failed = self.repository.fail_execution(
                action.action_id,
                idempotency_key,
                note=reason,
            )
            self._audit_outcome(
                failed,
                "preflight failure",
                outcome=ActionGraphExecutionOutcome.FAILED,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=0,
                note=reason,
            )
            return ActionGraphExecutionReport(
                action_id=action.action_id,
                outcome=ActionGraphExecutionOutcome.FAILED,
                final_status=failed.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=0,
                reason=reason,
            )

        conflict_reasons: list[str] = []
        if action.current_snapshot.change_key is None:
            conflict_reasons.append("approved snapshot has no changeKey")
        elif live.change_key != action.current_snapshot.change_key:
            conflict_reasons.append("message changeKey changed after approval")
        if _category_keys(live.categories) != _category_keys(action.current_snapshot.categories):
            conflict_reasons.append("message categories changed after approval")

        if conflict_reasons:
            reason = _safe_reason(f"Preflight conflict ({'; '.join(conflict_reasons)})")
            failed = self.repository.fail_execution(
                action.action_id,
                idempotency_key,
                note=reason,
            )
            self._audit_outcome(
                failed,
                "preflight conflict",
                outcome=ActionGraphExecutionOutcome.CONFLICT,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=0,
                note=reason,
            )
            return ActionGraphExecutionReport(
                action_id=action.action_id,
                outcome=ActionGraphExecutionOutcome.CONFLICT,
                final_status=failed.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=0,
                reason=reason,
            )

        final_categories = _final_categories(action)
        if _category_keys(live.categories) == _category_keys(final_categories):
            succeeded = self.repository.complete_execution(action.action_id, idempotency_key)
            self._audit_outcome(
                succeeded,
                "no-change completion",
                outcome=ActionGraphExecutionOutcome.NO_CHANGE,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=0,
                note="Execution confirmed that categories already match the approved plan",
            )
            return ActionGraphExecutionReport(
                action_id=action.action_id,
                outcome=ActionGraphExecutionOutcome.NO_CHANGE,
                final_status=succeeded.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=0,
            )

        write_request = GraphCategoryWriteRequest(
            message_id=action.message_id,
            categories=final_categories,
        )
        try:
            in_flight = self.repository.mark_write_in_flight(action.action_id, idempotency_key)
        except ActionQueueStorageError as error:
            raise ActionExecutionPersistenceError(
                "Write-in-flight state could not be persisted; no Graph PATCH was sent"
            ) from error
        self._audit(in_flight, "write-in-flight transition")
        try:
            self.graph_client.set_categories(write_request, token)
        except GraphWriteOutcomeUnknownError as error:
            reason = _safe_reason("Graph category write outcome is unknown", error)
            try:
                unknown = self.repository.mark_execution_unknown(
                    action.action_id,
                    idempotency_key,
                    note=reason,
                )
            except ActionQueueStorageError as storage_error:
                raise ActionExecutionPersistenceError(
                    "Graph write outcome is unknown and queue state could not be persisted"
                ) from storage_error
            self._audit_outcome(
                unknown,
                "unknown write outcome",
                outcome=ActionGraphExecutionOutcome.OUTCOME_UNKNOWN,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=1,
                note=reason,
            )
            return ActionGraphExecutionReport(
                action_id=action.action_id,
                outcome=ActionGraphExecutionOutcome.OUTCOME_UNKNOWN,
                final_status=unknown.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=1,
                reason=reason,
            )
        except GraphRequestError as error:
            reason = _safe_reason("Graph category write failed", error)
            failed = self.repository.fail_execution(
                action.action_id,
                idempotency_key,
                note=reason,
            )
            self._audit_outcome(
                failed,
                "write failure",
                outcome=ActionGraphExecutionOutcome.FAILED,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=1,
                note=reason,
            )
            return ActionGraphExecutionReport(
                action_id=action.action_id,
                outcome=ActionGraphExecutionOutcome.FAILED,
                final_status=failed.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=1,
                reason=reason,
            )

        try:
            succeeded = self.repository.complete_execution(action.action_id, idempotency_key)
        except ActionQueueStorageError as error:
            raise ActionExecutionPersistenceError(
                "Graph categories were updated but queue success could not be persisted"
            ) from error
        self._audit_outcome(
            succeeded,
            "verified write completion",
            outcome=ActionGraphExecutionOutcome.SUCCEEDED,
            attempt_number=claim.attempt_number,
            graph_read_request_count=1,
            graph_write_request_count=1,
            note="Execution verified the Graph category update response",
        )
        return ActionGraphExecutionReport(
            action_id=action.action_id,
            outcome=ActionGraphExecutionOutcome.SUCCEEDED,
            final_status=succeeded.status,
            attempt_number=claim.attempt_number,
            graph_read_request_count=1,
            graph_write_request_count=1,
        )


class ActionReconciliationOutcome(StrEnum):
    """Read-only conclusions for a previously uncertain Graph write."""

    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    CONFLICT = "conflict"
    READ_FAILED = "read_failed"


class ActionReconciliationReport(FrozenModel):
    """Privacy-bounded reconciliation result with a zero-write proof."""

    action_id: str = Field(min_length=1, max_length=128)
    outcome: ActionReconciliationOutcome
    final_status: MailboxActionStatus
    graph_read_request_count: int = Field(default=1, ge=1, le=1)
    graph_write_request_count: int = Field(default=0, ge=0, le=0)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_reconciliation(self) -> Self:
        expected_status = {
            ActionReconciliationOutcome.APPLIED: MailboxActionStatus.SUCCEEDED,
            ActionReconciliationOutcome.NOT_APPLIED: MailboxActionStatus.FAILED,
            ActionReconciliationOutcome.CONFLICT: MailboxActionStatus.FAILED,
        }.get(self.outcome)
        if expected_status is not None and self.final_status is not expected_status:
            raise ValueError("reconciliation outcome does not match final status")
        if self.outcome is ActionReconciliationOutcome.READ_FAILED and self.final_status not in {
            MailboxActionStatus.WRITE_IN_FLIGHT,
            MailboxActionStatus.OUTCOME_UNKNOWN,
        }:
            raise ValueError("failed reconciliation must preserve an uncertain status")
        return self


class UncertainActionReconciler:
    """Resolve an uncertain category write using one read and zero writes."""

    def __init__(
        self,
        repository: ActionQueueRepository,
        graph_client: CategoryGraphClientProtocol,
        audit_log: ActionAuditLogProtocol,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.repository = repository
        self.graph_client = graph_client
        self.audit_log = audit_log
        self.clock = clock

    def _audit(self, action: MailboxAction, phase: str) -> None:
        try:
            self.audit_log.append_unique(audit_events_for_action(action))
        except ActionAuditStorageError as error:
            raise ActionExecutionAuditError(
                f"Unable to persist reconciliation audit after {phase}: {action.action_id}"
            ) from error

    def _audit_outcome(
        self,
        action: MailboxAction,
        phase: str,
        *,
        outcome: ActionReconciliationOutcome,
        occurred_at: datetime,
        note: str,
    ) -> None:
        operation_event = audit_event_for_graph_operation(
            action,
            occurred_at=occurred_at,
            operation=AuditGraphOperation.RECONCILE,
            outcome=AuditGraphOutcome(outcome.value),
            attempt_number=_attempt_number(action),
            graph_read_request_count=1,
            graph_write_request_count=0,
            note=note,
        )
        try:
            self.audit_log.append_unique((*audit_events_for_action(action), operation_event))
        except ActionAuditStorageError as error:
            raise ActionExecutionAuditError(
                f"Unable to persist reconciliation audit after {phase}: {action.action_id}"
            ) from error

    def reconcile(
        self,
        action_id: str,
        idempotency_key: str,
        token: GraphAccessToken,
    ) -> ActionReconciliationReport:
        """Read live categories and resolve without sending any Graph mutation."""

        action = self.repository.get_uncertain_execution(action_id, idempotency_key)
        self._audit(action, "uncertain-state load")
        try:
            live = self.graph_client.get_category_snapshot(action.message_id, token)
        except GraphRequestError as error:
            reason = _safe_reason("Graph reconciliation read failed", error)
            self._audit_outcome(
                action,
                "reconciliation read failure",
                outcome=ActionReconciliationOutcome.READ_FAILED,
                occurred_at=self.clock(),
                note=reason,
            )
            return ActionReconciliationReport(
                action_id=action.action_id,
                outcome=ActionReconciliationOutcome.READ_FAILED,
                final_status=action.status,
                reason=reason,
            )

        final_categories = _final_categories(action)
        live_keys = _category_keys(live.categories)
        target_status: Literal[
            MailboxActionStatus.SUCCEEDED,
            MailboxActionStatus.FAILED,
        ]
        if live_keys == _category_keys(final_categories):
            outcome = ActionReconciliationOutcome.APPLIED
            target_status = MailboxActionStatus.SUCCEEDED
            reason = "Reconciliation confirmed the intended categories are applied"
        elif live_keys == _category_keys(action.current_snapshot.categories):
            outcome = ActionReconciliationOutcome.NOT_APPLIED
            target_status = MailboxActionStatus.FAILED
            reason = "Reconciliation confirmed the intended categories are not applied"
        else:
            outcome = ActionReconciliationOutcome.CONFLICT
            target_status = MailboxActionStatus.FAILED
            reason = "Reconciliation found categories different from both approved states"

        resolved = self.repository.resolve_uncertain_execution(
            action.action_id,
            idempotency_key,
            target_status,
            note=reason,
        )
        self._audit_outcome(
            resolved,
            "reconciliation resolution",
            outcome=outcome,
            occurred_at=resolved.updated_at,
            note=reason,
        )
        return ActionReconciliationReport(
            action_id=action.action_id,
            outcome=outcome,
            final_status=resolved.status,
            reason=reason,
        )
