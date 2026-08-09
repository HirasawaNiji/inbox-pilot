"""Private JSON persistence and construction for the human review queue."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from inbox_agent.actions.locking import ActionFileLock, ActionFileLockError
from inbox_agent.actions.models import (
    ActionActor,
    ActionEvidence,
    CategoryWritePlan,
    MailboxAction,
    MailboxActionStatus,
    MailboxActionType,
    OutlookCategorySnapshot,
    RollbackExecutionSnapshot,
    build_action_idempotency_key,
)
from inbox_agent.models import FrozenModel, MessageDataset
from inbox_agent.pipeline import AnalysisReport


class ActionQueueStorageError(Exception):
    """Raised when the private action queue cannot be loaded or saved."""


class ActionQueueNotFoundError(ActionQueueStorageError):
    """Raised when a requested action ID is absent from the queue."""


class ActionQueueConflictError(ActionQueueStorageError):
    """Raised when an existing action ID refers to different content."""


class ActionExecutionGuardError(ActionQueueStorageError):
    """Raised when a mailbox execution cannot be safely claimed or finalized."""


class ActionExecutionInProgressError(ActionExecutionGuardError):
    """Raised when a non-stale execution lease is already active."""


class ExecutionClaimOutcome(StrEnum):
    """Result of atomically claiming one action for a future executor."""

    CLAIMED = "claimed"
    RETRY_CLAIMED = "retry_claimed"
    ALREADY_SUCCEEDED = "already_succeeded"


class RollbackClaimOutcome(StrEnum):
    """Result of atomically claiming a succeeded action for rollback."""

    CLAIMED = "claimed"
    RETRY_CLAIMED = "retry_claimed"
    ALREADY_ROLLED_BACK = "already_rolled_back"


class ActionBuildError(Exception):
    """Raised when analysis evidence cannot produce safe review actions."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value


class ActionQueue(FrozenModel):
    """Versioned private collection of reviewable mailbox actions."""

    schema_version: Literal["1.0"] = "1.0"
    updated_at: datetime
    actions: tuple[MailboxAction, ...] = ()

    @field_validator("updated_at")
    @classmethod
    def validate_updated_at(cls, value: datetime) -> datetime:
        """Require an absolute queue update timestamp."""

        return _require_aware(value, "action queue timestamp")

    @model_validator(mode="after")
    def validate_unique_action_ids(self) -> Self:
        """Prevent ambiguous lookup and accidental duplicate persistence."""

        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("action queue contains duplicate action IDs")
        return self

    @property
    def pending_count(self) -> int:
        """Return actions awaiting a human decision."""

        return sum(action.status is MailboxActionStatus.PENDING_REVIEW for action in self.actions)

    def find(self, action_id: str) -> MailboxAction | None:
        """Return one action without changing queue state."""

        return next((action for action in self.actions if action.action_id == action_id), None)


class QueueUpdateReport(FrozenModel):
    """Counts returned after adding generated actions to the queue."""

    generated_count: int = Field(ge=0)
    added_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)
    queue_path: Path
    added_action_ids: tuple[str, ...] = ()
    skipped_action_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_action_id_counts(self) -> Self:
        """Keep mutation counts aligned with their traceable action IDs."""

        if self.added_count != len(self.added_action_ids):
            raise ValueError("added_count must match added_action_ids")
        if self.skipped_count != len(self.skipped_action_ids):
            raise ValueError("skipped_count must match skipped_action_ids")
        return self


class ExecutionClaim(FrozenModel):
    """Atomic execution decision returned to a future Graph writer."""

    outcome: ExecutionClaimOutcome
    action: MailboxAction
    attempt_number: int = Field(ge=0)
    should_execute: bool
    idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        """Align the result flag and action state with the claim outcome."""

        already_succeeded = self.outcome is ExecutionClaimOutcome.ALREADY_SUCCEEDED
        if self.should_execute is already_succeeded:
            raise ValueError("execution claim outcome does not match should_execute")
        expected_status = (
            MailboxActionStatus.SUCCEEDED if already_succeeded else MailboxActionStatus.EXECUTING
        )
        if self.action.status is not expected_status:
            raise ValueError("execution claim outcome does not match action status")
        return self


class RollbackClaim(FrozenModel):
    """Atomic decision for one explicitly requested rollback attempt."""

    outcome: RollbackClaimOutcome
    action: MailboxAction
    attempt_number: int = Field(ge=0)
    should_execute: bool
    rollback_idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_claim(self) -> Self:
        already = self.outcome is RollbackClaimOutcome.ALREADY_ROLLED_BACK
        if self.should_execute is already:
            raise ValueError("rollback claim outcome does not match should_execute")
        expected = (
            MailboxActionStatus.ROLLED_BACK if already else MailboxActionStatus.ROLLBACK_EXECUTING
        )
        if self.action.status is not expected:
            raise ValueError("rollback claim outcome does not match action status")
        return self


class ActionQueueRepository:
    """Load and atomically update a private JSON action queue."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self.path = Path(path)
        self.clock = clock
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.lock_timeout_seconds = lock_timeout_seconds

    def _lock(self) -> ActionFileLock:
        return ActionFileLock(
            self.lock_path,
            timeout_seconds=self.lock_timeout_seconds,
        )

    def load(self) -> ActionQueue:
        """Load the queue, treating a missing file as an empty queue."""

        try:
            with self._lock():
                return self._load_unlocked()
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def _load_unlocked(self) -> ActionQueue:
        """Load queue state while the caller holds the repository lock."""

        try:
            raw_content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ActionQueue(updated_at=self.clock())
        except UnicodeDecodeError as error:
            raise ActionQueueStorageError(
                f"Action queue is not valid UTF-8: {self.path}"
            ) from error
        except OSError as error:
            raise ActionQueueStorageError(f"Unable to read action queue: {self.path}") from error

        try:
            return ActionQueue.model_validate_json(raw_content)
        except ValidationError as error:
            raise ActionQueueStorageError(
                f"Action queue does not match the InboxPilot schema: {self.path}"
            ) from error

    def save(self, queue: ActionQueue) -> None:
        """Atomically replace the private queue JSON file."""

        try:
            with self._lock():
                self._save_unlocked(queue)
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def _save_unlocked(self, queue: ActionQueue) -> None:
        """Atomically save queue state while the caller holds the repository lock."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f".{self.path.name}.tmp")
        try:
            temporary_path.write_text(
                json.dumps(queue.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(self.path)
        except OSError as error:
            raise ActionQueueStorageError(f"Unable to write action queue: {self.path}") from error
        finally:
            temporary_path.unlink(missing_ok=True)

    def enqueue(self, actions: Iterable[MailboxAction]) -> QueueUpdateReport:
        """Add new actions while treating identical IDs as safe rebuilds."""

        generated = tuple(actions)
        try:
            with self._lock():
                queue = self._load_unlocked()
                existing_by_id = {action.action_id: action for action in queue.actions}
                added: list[MailboxAction] = []
                skipped_action_ids: list[str] = []

                for action in generated:
                    existing = existing_by_id.get(action.action_id)
                    if existing is None:
                        existing_by_id[action.action_id] = action
                        added.append(action)
                        continue
                    if not _same_proposal(existing, action):
                        raise ActionQueueConflictError(
                            f"Action ID already exists with different content: {action.action_id}"
                        )
                    skipped_action_ids.append(action.action_id)

                if added:
                    next_queue = ActionQueue(
                        updated_at=self.clock(),
                        actions=tuple(
                            sorted(
                                (*queue.actions, *added),
                                key=lambda item: (item.created_at, item.action_id),
                            )
                        ),
                    )
                    self._save_unlocked(next_queue)
                else:
                    next_queue = queue
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

        return QueueUpdateReport(
            generated_count=len(generated),
            added_count=len(added),
            skipped_count=len(skipped_action_ids),
            total_count=len(next_queue.actions),
            pending_count=next_queue.pending_count,
            queue_path=self.path,
            added_action_ids=tuple(action.action_id for action in added),
            skipped_action_ids=tuple(skipped_action_ids),
        )

    def transition(
        self,
        action_id: str,
        to_status: MailboxActionStatus,
        *,
        actor: ActionActor,
        note: str | None = None,
    ) -> MailboxAction:
        """Advance one action and persist the validated replacement."""

        try:
            with self._lock():
                queue = self._load_unlocked()
                current = self._require_action(queue, action_id)
                occurred_at = self.clock()
                updated = current.transition(
                    to_status,
                    occurred_at=occurred_at,
                    actor=actor,
                    note=note,
                )
                self._replace_unlocked(queue, updated, occurred_at)
                return updated
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def claim_execution(
        self,
        action_id: str,
        idempotency_key: str,
        *,
        stale_after_seconds: float | None = None,
    ) -> ExecutionClaim:
        """Atomically claim an approved/failed action or safely no-op a success."""

        if stale_after_seconds is not None and stale_after_seconds < 0:
            raise ValueError("stale execution threshold must not be negative")
        try:
            with self._lock():
                queue = self._load_unlocked()
                current = self._require_action(queue, action_id)
                self._require_idempotency_key(current, idempotency_key)

                if current.status is MailboxActionStatus.SUCCEEDED:
                    return self._execution_claim(
                        current,
                        ExecutionClaimOutcome.ALREADY_SUCCEEDED,
                        should_execute=False,
                    )

                now = self.clock()
                outcome = ExecutionClaimOutcome.CLAIMED
                if current.status is MailboxActionStatus.EXECUTING:
                    if stale_after_seconds is None:
                        raise ActionExecutionInProgressError(
                            f"Action execution is already in progress: {action_id}"
                        )
                    age_seconds = (now - current.updated_at).total_seconds()
                    if age_seconds < stale_after_seconds:
                        raise ActionExecutionInProgressError(
                            f"Action execution lease is not stale: {action_id}"
                        )
                    current = current.transition(
                        MailboxActionStatus.FAILED,
                        occurred_at=now,
                        actor=ActionActor.SYSTEM,
                        note=(f"Execution lease expired after {max(age_seconds, 0):.3f} seconds"),
                    )
                    outcome = ExecutionClaimOutcome.RETRY_CLAIMED
                elif current.status is MailboxActionStatus.FAILED:
                    outcome = ExecutionClaimOutcome.RETRY_CLAIMED
                elif current.status is not MailboxActionStatus.APPROVED:
                    raise ActionExecutionGuardError(
                        f"Action cannot be executed from status {current.status}: {action_id}"
                    )

                claimed = current.transition(
                    MailboxActionStatus.EXECUTING,
                    occurred_at=now,
                    actor=ActionActor.SYSTEM,
                )
                self._replace_unlocked(queue, claimed, now)
                return self._execution_claim(claimed, outcome, should_execute=True)
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def complete_execution(self, action_id: str, idempotency_key: str) -> MailboxAction:
        """Atomically record successful completion of the active execution."""

        return self._finalize_execution(
            action_id,
            idempotency_key,
            MailboxActionStatus.SUCCEEDED,
        )

    def mark_write_in_flight(self, action_id: str, idempotency_key: str) -> MailboxAction:
        """Persist the no-blind-retry boundary immediately before Graph PATCH."""

        try:
            with self._lock():
                queue = self._load_unlocked()
                current = self._require_action(queue, action_id)
                self._require_idempotency_key(current, idempotency_key)
                if current.status is not MailboxActionStatus.EXECUTING:
                    raise ActionExecutionGuardError(
                        f"Action has no active preflight execution: {action_id}"
                    )
                occurred_at = self.clock()
                updated = current.transition(
                    MailboxActionStatus.WRITE_IN_FLIGHT,
                    occurred_at=occurred_at,
                    actor=ActionActor.SYSTEM,
                )
                self._replace_unlocked(queue, updated, occurred_at)
                return updated
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def fail_execution(
        self,
        action_id: str,
        idempotency_key: str,
        *,
        note: str,
    ) -> MailboxAction:
        """Atomically record a retryable failure of the active execution."""

        return self._finalize_execution(
            action_id,
            idempotency_key,
            MailboxActionStatus.FAILED,
            note=note,
        )

    def mark_execution_unknown(
        self,
        action_id: str,
        idempotency_key: str,
        *,
        note: str,
    ) -> MailboxAction:
        """Block blind retries when Graph may have accepted the write."""

        return self._finalize_execution(
            action_id,
            idempotency_key,
            MailboxActionStatus.OUTCOME_UNKNOWN,
            note=note,
        )

    def get_uncertain_execution(
        self,
        action_id: str,
        idempotency_key: str,
    ) -> MailboxAction:
        """Load one write-in-flight/unknown action for read-only reconciliation."""

        try:
            with self._lock():
                queue = self._load_unlocked()
                current = self._require_action(queue, action_id)
                self._require_idempotency_key(current, idempotency_key)
                if current.status not in {
                    MailboxActionStatus.WRITE_IN_FLIGHT,
                    MailboxActionStatus.OUTCOME_UNKNOWN,
                }:
                    raise ActionExecutionGuardError(
                        f"Action does not require execution reconciliation: {action_id}"
                    )
                return current
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def resolve_uncertain_execution(
        self,
        action_id: str,
        idempotency_key: str,
        status: Literal[MailboxActionStatus.SUCCEEDED, MailboxActionStatus.FAILED],
        *,
        note: str,
    ) -> MailboxAction:
        """Atomically resolve a Graph outcome after a live read-only comparison."""

        try:
            with self._lock():
                queue = self._load_unlocked()
                current = self._require_action(queue, action_id)
                self._require_idempotency_key(current, idempotency_key)
                if current.status not in {
                    MailboxActionStatus.WRITE_IN_FLIGHT,
                    MailboxActionStatus.OUTCOME_UNKNOWN,
                }:
                    raise ActionExecutionGuardError(
                        f"Action does not require execution reconciliation: {action_id}"
                    )
                occurred_at = self.clock()
                updated = current.transition(
                    status,
                    occurred_at=occurred_at,
                    actor=ActionActor.SYSTEM,
                    note=note,
                )
                self._replace_unlocked(queue, updated, occurred_at)
                return updated
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def _finalize_execution(
        self,
        action_id: str,
        idempotency_key: str,
        status: Literal[
            MailboxActionStatus.SUCCEEDED,
            MailboxActionStatus.FAILED,
            MailboxActionStatus.OUTCOME_UNKNOWN,
        ],
        *,
        note: str | None = None,
    ) -> MailboxAction:
        try:
            with self._lock():
                queue = self._load_unlocked()
                current = self._require_action(queue, action_id)
                self._require_idempotency_key(current, idempotency_key)
                if current.status not in {
                    MailboxActionStatus.EXECUTING,
                    MailboxActionStatus.WRITE_IN_FLIGHT,
                }:
                    raise ActionExecutionGuardError(
                        f"Action has no active execution to finalize: {action_id}"
                    )
                occurred_at = self.clock()
                updated = current.transition(
                    status,
                    occurred_at=occurred_at,
                    actor=ActionActor.SYSTEM,
                    note=note,
                )
                self._replace_unlocked(queue, updated, occurred_at)
                return updated
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def claim_rollback(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        *,
        reason: str,
    ) -> RollbackClaim:
        """Atomically claim one succeeded/failed rollback or no-op a completed one."""

        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ActionExecutionGuardError("Rollback reason must not be empty")
        self._validate_rollback_key(rollback_idempotency_key)
        try:
            with self._lock():
                queue = self._load_unlocked()
                current = self._require_action(queue, action_id)
                self._require_rollback_identity(
                    current,
                    rollback_idempotency_key,
                    normalized_reason,
                )
                if current.status is MailboxActionStatus.ROLLED_BACK:
                    return self._rollback_claim(
                        current,
                        rollback_idempotency_key,
                        RollbackClaimOutcome.ALREADY_ROLLED_BACK,
                        should_execute=False,
                    )

                outcome = RollbackClaimOutcome.CLAIMED
                if current.status is MailboxActionStatus.ROLLBACK_FAILED:
                    outcome = RollbackClaimOutcome.RETRY_CLAIMED
                elif current.status is not MailboxActionStatus.SUCCEEDED:
                    raise ActionExecutionGuardError(
                        f"Action cannot be rolled back from status {current.status}: {action_id}"
                    )
                occurred_at = self.clock()
                claimed = current.transition(
                    MailboxActionStatus.ROLLBACK_EXECUTING,
                    occurred_at=occurred_at,
                    actor=ActionActor.SYSTEM,
                    note=f"Rollback requested: {normalized_reason}",
                )
                self._replace_unlocked(queue, claimed, occurred_at)
                return self._rollback_claim(
                    claimed,
                    rollback_idempotency_key,
                    outcome,
                    should_execute=True,
                )
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def mark_rollback_write_in_flight(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        snapshot: RollbackExecutionSnapshot,
    ) -> MailboxAction:
        """Persist the exact rollback comparison states before the single PATCH."""

        return self._prepare_or_complete_rollback(
            action_id,
            rollback_idempotency_key,
            snapshot,
            MailboxActionStatus.ROLLBACK_WRITE_IN_FLIGHT,
        )

    def complete_rollback_without_write(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        snapshot: RollbackExecutionSnapshot,
        *,
        note: str,
    ) -> MailboxAction:
        """Record a verified no-change rollback with its live snapshot."""

        return self._prepare_or_complete_rollback(
            action_id,
            rollback_idempotency_key,
            snapshot,
            MailboxActionStatus.ROLLED_BACK,
            note=note,
        )

    def _prepare_or_complete_rollback(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        snapshot: RollbackExecutionSnapshot,
        status: Literal[
            MailboxActionStatus.ROLLBACK_WRITE_IN_FLIGHT,
            MailboxActionStatus.ROLLED_BACK,
        ],
        *,
        note: str | None = None,
    ) -> MailboxAction:
        self._validate_rollback_key(rollback_idempotency_key)
        if snapshot.rollback_idempotency_key != rollback_idempotency_key:
            raise ActionExecutionGuardError("Rollback snapshot idempotency key does not match")
        try:
            with self._lock():
                queue = self._load_unlocked()
                current = self._require_action(queue, action_id)
                if current.status is not MailboxActionStatus.ROLLBACK_EXECUTING:
                    raise ActionExecutionGuardError(
                        f"Action has no active rollback preflight: {action_id}"
                    )
                prepared = current.model_copy(update={"rollback_snapshot": snapshot})
                occurred_at = self.clock()
                updated = prepared.transition(
                    status,
                    occurred_at=occurred_at,
                    actor=ActionActor.SYSTEM,
                    note=note,
                )
                self._replace_unlocked(queue, updated, occurred_at)
                return updated
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def complete_rollback(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        *,
        note: str,
    ) -> MailboxAction:
        return self._finalize_rollback(
            action_id,
            rollback_idempotency_key,
            MailboxActionStatus.ROLLED_BACK,
            note=note,
        )

    def fail_rollback(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        *,
        note: str,
    ) -> MailboxAction:
        return self._finalize_rollback(
            action_id,
            rollback_idempotency_key,
            MailboxActionStatus.ROLLBACK_FAILED,
            note=note,
        )

    def mark_rollback_unknown(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        *,
        note: str,
    ) -> MailboxAction:
        return self._finalize_rollback(
            action_id,
            rollback_idempotency_key,
            MailboxActionStatus.ROLLBACK_OUTCOME_UNKNOWN,
            note=note,
        )

    def get_uncertain_rollback(
        self,
        action_id: str,
        rollback_idempotency_key: str,
    ) -> MailboxAction:
        """Load an uncertain rollback for a zero-write live comparison."""

        try:
            with self._lock():
                queue = self._load_unlocked()
                current = self._require_action(queue, action_id)
                self._require_rollback_identity(current, rollback_idempotency_key, None)
                if current.status not in {
                    MailboxActionStatus.ROLLBACK_WRITE_IN_FLIGHT,
                    MailboxActionStatus.ROLLBACK_OUTCOME_UNKNOWN,
                }:
                    raise ActionExecutionGuardError(
                        f"Action does not require rollback reconciliation: {action_id}"
                    )
                return current
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    def resolve_uncertain_rollback(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        status: Literal[MailboxActionStatus.ROLLED_BACK, MailboxActionStatus.ROLLBACK_FAILED],
        *,
        note: str,
    ) -> MailboxAction:
        """Resolve an uncertain rollback after one read and no writes."""

        return self._finalize_rollback(
            action_id,
            rollback_idempotency_key,
            status,
            note=note,
            allowed_from={
                MailboxActionStatus.ROLLBACK_WRITE_IN_FLIGHT,
                MailboxActionStatus.ROLLBACK_OUTCOME_UNKNOWN,
            },
        )

    def _finalize_rollback(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        status: Literal[
            MailboxActionStatus.ROLLED_BACK,
            MailboxActionStatus.ROLLBACK_FAILED,
            MailboxActionStatus.ROLLBACK_OUTCOME_UNKNOWN,
        ],
        *,
        note: str,
        allowed_from: set[MailboxActionStatus] | None = None,
    ) -> MailboxAction:
        try:
            with self._lock():
                queue = self._load_unlocked()
                current = self._require_action(queue, action_id)
                self._require_rollback_identity(current, rollback_idempotency_key, None)
                valid_from = allowed_from or {
                    MailboxActionStatus.ROLLBACK_EXECUTING,
                    MailboxActionStatus.ROLLBACK_WRITE_IN_FLIGHT,
                }
                if current.status not in valid_from:
                    raise ActionExecutionGuardError(
                        f"Action has no active rollback to finalize: {action_id}"
                    )
                occurred_at = self.clock()
                updated = current.transition(
                    status,
                    occurred_at=occurred_at,
                    actor=ActionActor.SYSTEM,
                    note=note,
                )
                self._replace_unlocked(queue, updated, occurred_at)
                return updated
        except ActionFileLockError as error:
            raise ActionQueueStorageError(f"Unable to lock action queue: {self.path}") from error

    @staticmethod
    def _require_action(queue: ActionQueue, action_id: str) -> MailboxAction:
        current = queue.find(action_id)
        if current is None:
            raise ActionQueueNotFoundError(f"Action does not exist: {action_id}")
        return current

    @staticmethod
    def _require_idempotency_key(action: MailboxAction, supplied_key: str) -> None:
        if action.idempotency_key is None:
            raise ActionExecutionGuardError(
                f"Action has no idempotency key and cannot be executed: {action.action_id}"
            )
        if action.idempotency_key != supplied_key:
            raise ActionExecutionGuardError(
                f"Idempotency key does not match action: {action.action_id}"
            )

    @staticmethod
    def _validate_rollback_key(supplied_key: str) -> None:
        invalid_character = any(character not in "0123456789abcdef" for character in supplied_key)
        if len(supplied_key) != 64 or invalid_character:
            raise ActionExecutionGuardError(
                "Rollback idempotency key must be 64 lowercase hex characters"
            )

    @classmethod
    def _require_rollback_identity(
        cls,
        action: MailboxAction,
        supplied_key: str,
        reason: str | None,
    ) -> None:
        cls._validate_rollback_key(supplied_key)
        snapshot = action.rollback_snapshot
        if snapshot is not None and snapshot.rollback_idempotency_key != supplied_key:
            raise ActionExecutionGuardError(
                f"Rollback idempotency key does not match action: {action.action_id}"
            )
        if snapshot is not None and reason is not None and snapshot.reason != reason:
            raise ActionExecutionGuardError(
                f"Rollback reason does not match the original attempt: {action.action_id}"
            )

    @staticmethod
    def _execution_claim(
        action: MailboxAction,
        outcome: ExecutionClaimOutcome,
        *,
        should_execute: bool,
    ) -> ExecutionClaim:
        assert action.idempotency_key is not None
        attempt_number = sum(
            transition.to_status is MailboxActionStatus.EXECUTING
            for transition in action.transition_history
        )
        return ExecutionClaim(
            outcome=outcome,
            action=action,
            attempt_number=attempt_number,
            should_execute=should_execute,
            idempotency_key=action.idempotency_key,
        )

    @staticmethod
    def _rollback_claim(
        action: MailboxAction,
        rollback_idempotency_key: str,
        outcome: RollbackClaimOutcome,
        *,
        should_execute: bool,
    ) -> RollbackClaim:
        attempt_number = sum(
            transition.to_status is MailboxActionStatus.ROLLBACK_EXECUTING
            for transition in action.transition_history
        )
        return RollbackClaim(
            outcome=outcome,
            action=action,
            attempt_number=attempt_number,
            should_execute=should_execute,
            rollback_idempotency_key=rollback_idempotency_key,
        )

    def _replace_unlocked(
        self,
        queue: ActionQueue,
        updated: MailboxAction,
        occurred_at: datetime,
    ) -> None:
        actions = tuple(
            updated if action.action_id == updated.action_id else action for action in queue.actions
        )
        self._save_unlocked(ActionQueue(updated_at=occurred_at, actions=actions))


def _same_proposal(existing: MailboxAction, candidate: MailboxAction) -> bool:
    """Compare stable proposal inputs while ignoring lifecycle timestamps and status."""

    return (
        existing.message_id == candidate.message_id
        and existing.action_type is candidate.action_type
        and existing.current_snapshot.categories == candidate.current_snapshot.categories
        and existing.current_snapshot.change_key == candidate.current_snapshot.change_key
        and existing.write_plan == candidate.write_plan
        and existing.evidence.triage_result.priority is candidate.evidence.triage_result.priority
        and existing.evidence.triage_result.category == candidate.evidence.triage_result.category
        and existing.evidence.triage_result.requires_review
        is candidate.evidence.triage_result.requires_review
        and existing.evidence.triage_result.policy_version
        == candidate.evidence.triage_result.policy_version
    )


def _action_id_payload(
    *,
    message_id: str,
    existing_categories: tuple[str, ...],
    change_key: str | None,
    managed_categories: tuple[str, ...],
    policy_version: str,
) -> Mapping[str, object]:
    return {
        "message_id": message_id,
        "existing_categories": existing_categories,
        "change_key": change_key,
        "managed_categories": managed_categories,
        "policy_version": policy_version,
    }


def _build_action_id(**payload: object) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]
    return f"action-{digest}"


def build_review_actions(
    dataset: MessageDataset,
    analysis: AnalysisReport,
) -> tuple[MailboxAction, ...]:
    """Build pending actions from successful analysis and provider snapshots."""

    if len(analysis.rule_evaluations) != len(analysis.results):
        raise ActionBuildError("Analysis report is missing aligned rule evaluations")

    messages_by_id = {message.source_id: message for message in dataset.messages}
    llm_by_id = {result.message_id: result for result in analysis.llm_analyses}
    actions: list[MailboxAction] = []

    for result, rule_evaluation in zip(
        analysis.results,
        analysis.rule_evaluations,
        strict=True,
    ):
        message = messages_by_id.get(result.message_id)
        if message is None:
            raise ActionBuildError(f"Dataset has no message for result: {result.message_id}")

        managed_categories = [
            f"InboxPilot/{result.priority.value}",
            f"InboxPilot/{result.category}",
        ]
        if result.requires_review:
            managed_categories.append("InboxPilot/review")
        categories = tuple(managed_categories)
        action_id = _build_action_id(
            **_action_id_payload(
                message_id=result.message_id,
                existing_categories=message.categories,
                change_key=message.change_key,
                managed_categories=categories,
                policy_version=result.policy_version,
            )
        )
        actions.append(
            MailboxAction(
                action_id=action_id,
                message_id=result.message_id,
                current_snapshot=OutlookCategorySnapshot(
                    categories=message.categories,
                    observed_at=analysis.evaluated_at,
                    change_key=message.change_key,
                ),
                write_plan=CategoryWritePlan(managed_categories=categories),
                evidence=ActionEvidence(
                    rule_evaluation=rule_evaluation,
                    llm_analysis=llm_by_id.get(result.message_id),
                    triage_result=result,
                ),
                created_at=analysis.evaluated_at,
                updated_at=analysis.evaluated_at,
                idempotency_key=build_action_idempotency_key(
                    message_id=result.message_id,
                    action_type=MailboxActionType.SET_CATEGORIES,
                    current_categories=message.categories,
                    change_key=message.change_key,
                    managed_categories=categories,
                    policy_version=result.policy_version,
                ),
            )
        )
    return tuple(actions)
