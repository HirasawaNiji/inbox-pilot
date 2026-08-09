"""Real, single-action Outlook category rollback with read-only reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from inbox_agent.actions.audit import (
    ActionAuditStorageError,
    AuditGraphOperation,
    AuditGraphOutcome,
    audit_event_for_graph_operation,
    audit_events_for_action,
)
from inbox_agent.actions.graph_executor import (
    ActionAuditLogProtocol,
    ActionExecutionAuditError,
    ActionExecutionPersistenceError,
    CategoryGraphClientProtocol,
)
from inbox_agent.actions.models import (
    MANAGED_CATEGORY_PREFIX,
    MailboxAction,
    MailboxActionStatus,
    RollbackExecutionSnapshot,
)
from inbox_agent.actions.queue import (
    ActionExecutionGuardError,
    ActionQueueRepository,
    ActionQueueStorageError,
)
from inbox_agent.actions.rollback import build_rollback_idempotency_key
from inbox_agent.graph import (
    GraphAccessToken,
    GraphCategoryWriteRequest,
    GraphRequestError,
    GraphWriteOutcomeUnknownError,
)
from inbox_agent.models import FrozenModel


class RollbackExecutionOutcome(StrEnum):
    """Bounded outcomes for one controlled rollback attempt."""

    ALREADY_ROLLED_BACK = "already_rolled_back"
    ROLLED_BACK = "rolled_back"
    NO_CHANGE = "no_change"
    CONFLICT = "conflict"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class RollbackExecutionReport(FrozenModel):
    """Privacy-safe rollback result and exact Graph request counts."""

    action_id: str = Field(min_length=1, max_length=128)
    outcome: RollbackExecutionOutcome
    final_status: MailboxActionStatus
    attempt_number: int = Field(ge=0)
    graph_read_request_count: int = Field(ge=0, le=1)
    graph_write_request_count: int = Field(ge=0, le=1)
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_outcome(self) -> RollbackExecutionReport:
        fixed = {
            RollbackExecutionOutcome.ALREADY_ROLLED_BACK: (
                MailboxActionStatus.ROLLED_BACK,
                0,
                0,
            ),
            RollbackExecutionOutcome.ROLLED_BACK: (
                MailboxActionStatus.ROLLED_BACK,
                1,
                1,
            ),
            RollbackExecutionOutcome.NO_CHANGE: (
                MailboxActionStatus.ROLLED_BACK,
                1,
                0,
            ),
            RollbackExecutionOutcome.CONFLICT: (
                MailboxActionStatus.ROLLBACK_FAILED,
                1,
                0,
            ),
            RollbackExecutionOutcome.OUTCOME_UNKNOWN: (
                MailboxActionStatus.ROLLBACK_OUTCOME_UNKNOWN,
                1,
                1,
            ),
        }.get(self.outcome)
        if fixed is not None:
            actual = (
                self.final_status,
                self.graph_read_request_count,
                self.graph_write_request_count,
            )
            if actual != fixed:
                raise ValueError("rollback report counts do not match the outcome")
        elif self.final_status is not MailboxActionStatus.ROLLBACK_FAILED:
            raise ValueError("failed rollback report must have rollback_failed status")
        if (
            self.outcome
            in {
                RollbackExecutionOutcome.CONFLICT,
                RollbackExecutionOutcome.FAILED,
                RollbackExecutionOutcome.OUTCOME_UNKNOWN,
            }
            and self.reason is None
        ):
            raise ValueError("non-success rollback report requires a reason")
        return self


class RollbackReconciliationOutcome(StrEnum):
    """Read-only conclusions for an uncertain rollback PATCH."""

    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    CONFLICT = "conflict"
    READ_FAILED = "read_failed"


class RollbackReconciliationReport(FrozenModel):
    """Zero-write result for one uncertain rollback comparison."""

    action_id: str = Field(min_length=1, max_length=128)
    outcome: RollbackReconciliationOutcome
    final_status: MailboxActionStatus
    graph_read_request_count: Literal[1] = 1
    graph_write_request_count: Literal[0] = 0
    reason: str = Field(min_length=1, max_length=500)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _keys(categories: tuple[str, ...]) -> frozenset[str]:
    return frozenset(category.casefold() for category in categories)


def _managed(categories: tuple[str, ...]) -> tuple[str, ...]:
    prefix = MANAGED_CATEGORY_PREFIX.casefold()
    return tuple(category for category in categories if category.casefold().startswith(prefix))


def _unmanaged(categories: tuple[str, ...]) -> tuple[str, ...]:
    prefix = MANAGED_CATEGORY_PREFIX.casefold()
    return tuple(category for category in categories if not category.casefold().startswith(prefix))


def _expected_key(action: MailboxAction) -> str:
    if action.idempotency_key is None:
        raise ActionExecutionGuardError("Succeeded action has no forward idempotency key")
    original = action.current_snapshot.categories
    expected_current = (*_unmanaged(original), *action.write_plan.managed_categories)
    return build_rollback_idempotency_key(
        action_id=action.action_id,
        forward_idempotency_key=action.idempotency_key,
        expected_current_categories=expected_current,
        final_categories=original,
    )


def _safe_reason(prefix: str, error: Exception | None = None) -> str:
    if error is None:
        return prefix[:500]
    detail = str(error).replace("\r", " ").replace("\n", " ")
    return f"{prefix}: {type(error).__name__}: {detail}"[:500]


class ControlledRollbackExecutor:
    """Restore only original InboxPilot categories using at most one PATCH."""

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
                f"Unable to persist rollback audit after {phase}: {action.action_id}"
            ) from error

    def _audit_outcome(
        self,
        action: MailboxAction,
        *,
        outcome: RollbackExecutionOutcome,
        attempt_number: int,
        reads: int,
        writes: int,
        note: str,
    ) -> None:
        event = audit_event_for_graph_operation(
            action,
            occurred_at=action.updated_at,
            operation=AuditGraphOperation.ROLLBACK,
            outcome=AuditGraphOutcome(outcome.value),
            attempt_number=attempt_number,
            graph_read_request_count=reads,
            graph_write_request_count=writes,
            note=note,
        )
        try:
            self.audit_log.append_unique((*audit_events_for_action(action), event))
        except ActionAuditStorageError as error:
            raise ActionExecutionAuditError(
                f"Unable to persist rollback outcome audit: {action.action_id}"
            ) from error

    def execute(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        reason: str,
        token: GraphAccessToken,
    ) -> RollbackExecutionReport:
        """Preflight one message and restore categories with at most one PATCH."""

        current = self.repository.load().find(action_id)
        if current is None:
            raise ActionExecutionGuardError(f"Action does not exist: {action_id}")
        if rollback_idempotency_key != _expected_key(current):
            raise ActionExecutionGuardError(
                "Rollback idempotency key does not match the restoration plan"
            )
        claim = self.repository.claim_rollback(
            action_id,
            rollback_idempotency_key,
            reason=reason,
        )
        if not claim.should_execute:
            self._audit_outcome(
                claim.action,
                outcome=RollbackExecutionOutcome.ALREADY_ROLLED_BACK,
                attempt_number=claim.attempt_number,
                reads=0,
                writes=0,
                note="Rollback skipped because this restoration already completed",
            )
            return RollbackExecutionReport(
                action_id=action_id,
                outcome=RollbackExecutionOutcome.ALREADY_ROLLED_BACK,
                final_status=claim.action.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=0,
                graph_write_request_count=0,
            )

        action = claim.action
        self._audit(action, "claim")
        try:
            live = self.graph_client.get_category_snapshot(action.message_id, token)
        except GraphRequestError as error:
            failure_reason = _safe_reason("Graph rollback preflight read failed", error)
            failed = self.repository.fail_rollback(
                action_id,
                rollback_idempotency_key,
                note=failure_reason,
            )
            self._audit_outcome(
                failed,
                outcome=RollbackExecutionOutcome.FAILED,
                attempt_number=claim.attempt_number,
                reads=1,
                writes=0,
                note=failure_reason,
            )
            return RollbackExecutionReport(
                action_id=action_id,
                outcome=RollbackExecutionOutcome.FAILED,
                final_status=failed.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=0,
                reason=failure_reason,
            )

        if _keys(_managed(live.categories)) != _keys(action.write_plan.managed_categories):
            conflict_reason = "Rollback preflight conflict: live InboxPilot categories changed"
            failed = self.repository.fail_rollback(
                action_id,
                rollback_idempotency_key,
                note=conflict_reason,
            )
            self._audit_outcome(
                failed,
                outcome=RollbackExecutionOutcome.CONFLICT,
                attempt_number=claim.attempt_number,
                reads=1,
                writes=0,
                note=conflict_reason,
            )
            return RollbackExecutionReport(
                action_id=action_id,
                outcome=RollbackExecutionOutcome.CONFLICT,
                final_status=failed.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=0,
                reason=conflict_reason,
            )

        target = (*_unmanaged(live.categories), *_managed(action.current_snapshot.categories))
        snapshot = RollbackExecutionSnapshot(
            rollback_idempotency_key=rollback_idempotency_key,
            reason=reason.strip(),
            observed_categories=live.categories,
            target_categories=target,
            observed_change_key=live.change_key,
            observed_at=self.clock(),
        )
        if _keys(live.categories) == _keys(target):
            completed = self.repository.complete_rollback_without_write(
                action_id,
                rollback_idempotency_key,
                snapshot,
                note="Rollback verified that the original managed categories are already restored",
            )
            self._audit_outcome(
                completed,
                outcome=RollbackExecutionOutcome.NO_CHANGE,
                attempt_number=claim.attempt_number,
                reads=1,
                writes=0,
                note="Rollback required no Graph write",
            )
            return RollbackExecutionReport(
                action_id=action_id,
                outcome=RollbackExecutionOutcome.NO_CHANGE,
                final_status=completed.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=0,
            )

        try:
            in_flight = self.repository.mark_rollback_write_in_flight(
                action_id,
                rollback_idempotency_key,
                snapshot,
            )
        except ActionQueueStorageError as error:
            raise ActionExecutionPersistenceError(
                "Rollback write-in-flight snapshot could not be persisted; no PATCH was sent"
            ) from error
        self._audit(in_flight, "write-in-flight")
        request = GraphCategoryWriteRequest(message_id=action.message_id, categories=target)
        try:
            self.graph_client.set_categories(request, token)
        except GraphWriteOutcomeUnknownError as error:
            failure_reason = _safe_reason("Graph rollback outcome is unknown", error)
            try:
                unknown = self.repository.mark_rollback_unknown(
                    action_id,
                    rollback_idempotency_key,
                    note=failure_reason,
                )
            except ActionQueueStorageError as storage_error:
                raise ActionExecutionPersistenceError(
                    "Rollback outcome is unknown and queue state could not be persisted"
                ) from storage_error
            self._audit_outcome(
                unknown,
                outcome=RollbackExecutionOutcome.OUTCOME_UNKNOWN,
                attempt_number=claim.attempt_number,
                reads=1,
                writes=1,
                note=failure_reason,
            )
            return RollbackExecutionReport(
                action_id=action_id,
                outcome=RollbackExecutionOutcome.OUTCOME_UNKNOWN,
                final_status=unknown.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=1,
                reason=failure_reason,
            )
        except GraphRequestError as error:
            failure_reason = _safe_reason("Graph rollback write failed", error)
            failed = self.repository.fail_rollback(
                action_id,
                rollback_idempotency_key,
                note=failure_reason,
            )
            self._audit_outcome(
                failed,
                outcome=RollbackExecutionOutcome.FAILED,
                attempt_number=claim.attempt_number,
                reads=1,
                writes=1,
                note=failure_reason,
            )
            return RollbackExecutionReport(
                action_id=action_id,
                outcome=RollbackExecutionOutcome.FAILED,
                final_status=failed.status,
                attempt_number=claim.attempt_number,
                graph_read_request_count=1,
                graph_write_request_count=1,
                reason=failure_reason,
            )

        try:
            completed = self.repository.complete_rollback(
                action_id,
                rollback_idempotency_key,
                note="Graph response verified the controlled category restoration",
            )
        except ActionQueueStorageError as error:
            raise ActionExecutionPersistenceError(
                "Graph rollback succeeded but queue completion could not be persisted"
            ) from error
        self._audit_outcome(
            completed,
            outcome=RollbackExecutionOutcome.ROLLED_BACK,
            attempt_number=claim.attempt_number,
            reads=1,
            writes=1,
            note="Rollback verified the Graph category restoration response",
        )
        return RollbackExecutionReport(
            action_id=action_id,
            outcome=RollbackExecutionOutcome.ROLLED_BACK,
            final_status=completed.status,
            attempt_number=claim.attempt_number,
            graph_read_request_count=1,
            graph_write_request_count=1,
        )


class UncertainRollbackReconciler:
    """Resolve an uncertain rollback with exactly one GET and zero PATCH calls."""

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

    def reconcile(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        token: GraphAccessToken,
    ) -> RollbackReconciliationReport:
        action = self.repository.get_uncertain_rollback(action_id, rollback_idempotency_key)
        snapshot = action.rollback_snapshot
        assert snapshot is not None
        try:
            live = self.graph_client.get_category_snapshot(action.message_id, token)
        except GraphRequestError as error:
            reason = _safe_reason("Graph rollback reconciliation read failed", error)
            self._audit_outcome(action, RollbackReconciliationOutcome.READ_FAILED, reason)
            return RollbackReconciliationReport(
                action_id=action_id,
                outcome=RollbackReconciliationOutcome.READ_FAILED,
                final_status=action.status,
                reason=reason,
            )

        live_keys = _keys(live.categories)
        if live_keys == _keys(snapshot.target_categories):
            outcome = RollbackReconciliationOutcome.APPLIED
            status: Literal[
                MailboxActionStatus.ROLLED_BACK,
                MailboxActionStatus.ROLLBACK_FAILED,
            ] = MailboxActionStatus.ROLLED_BACK
            reason = "Reconciliation confirmed the rollback target is applied"
        elif live_keys == _keys(snapshot.observed_categories):
            outcome = RollbackReconciliationOutcome.NOT_APPLIED
            status = MailboxActionStatus.ROLLBACK_FAILED
            reason = "Reconciliation confirmed the rollback was not applied"
        else:
            outcome = RollbackReconciliationOutcome.CONFLICT
            status = MailboxActionStatus.ROLLBACK_FAILED
            reason = "Reconciliation found categories different from both rollback states"
        resolved = self.repository.resolve_uncertain_rollback(
            action_id,
            rollback_idempotency_key,
            status,
            note=reason,
        )
        self._audit_outcome(resolved, outcome, reason)
        return RollbackReconciliationReport(
            action_id=action_id,
            outcome=outcome,
            final_status=resolved.status,
            reason=reason,
        )

    def _audit_outcome(
        self,
        action: MailboxAction,
        outcome: RollbackReconciliationOutcome,
        note: str,
    ) -> None:
        attempt = sum(
            transition.to_status is MailboxActionStatus.ROLLBACK_EXECUTING
            for transition in action.transition_history
        )
        event = audit_event_for_graph_operation(
            action,
            occurred_at=self.clock(),
            operation=AuditGraphOperation.ROLLBACK_RECONCILE,
            outcome=AuditGraphOutcome(outcome.value),
            attempt_number=attempt,
            graph_read_request_count=1,
            graph_write_request_count=0,
            note=note,
        )
        try:
            self.audit_log.append_unique((*audit_events_for_action(action), event))
        except ActionAuditStorageError as error:
            raise ActionExecutionAuditError(
                f"Unable to persist rollback reconciliation audit: {action.action_id}"
            ) from error
