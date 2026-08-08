"""Tests for idempotent execution claims and bounded retry recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from inbox_agent.actions import (
    ActionActor,
    ActionExecutionGuardError,
    ActionExecutionInProgressError,
    ActionQueueRepository,
    ExecutionClaimOutcome,
    MailboxAction,
    MailboxActionStatus,
    build_review_actions,
)
from inbox_agent.loader import load_dataset
from inbox_agent.pipeline import OfflinePipeline

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rules.yaml"
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
CREATED_AT = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)


def make_action() -> MailboxAction:
    dataset = load_dataset(DATASET_PATH)
    dataset = dataset.model_copy(update={"messages": dataset.messages[:1]})
    analysis = OfflinePipeline.from_yaml(POLICY_PATH).analyze_dataset(
        dataset,
        evaluated_at=CREATED_AT,
    )
    return build_review_actions(dataset, analysis)[0]


def approved_repository(
    tmp_path: Path,
) -> tuple[ActionQueueRepository, MailboxAction, list[datetime]]:
    current_time = [CREATED_AT + timedelta(minutes=1)]
    repository = ActionQueueRepository(
        tmp_path / "action_queue.json",
        clock=lambda: current_time[0],
    )
    action = make_action()
    repository.enqueue((action,))
    repository.transition(
        action.action_id,
        MailboxActionStatus.APPROVED,
        actor=ActionActor.USER,
    )
    return repository, action, current_time


def test_claim_failure_retry_success_and_successful_noop(tmp_path: Path) -> None:
    repository, action, current_time = approved_repository(tmp_path)
    assert action.idempotency_key is not None

    current_time[0] += timedelta(minutes=1)
    first = repository.claim_execution(action.action_id, action.idempotency_key)
    assert first.outcome is ExecutionClaimOutcome.CLAIMED
    assert first.should_execute is True
    assert first.attempt_number == 1

    current_time[0] += timedelta(minutes=1)
    failed = repository.fail_execution(
        action.action_id,
        action.idempotency_key,
        note="Temporary provider failure",
    )
    assert failed.status is MailboxActionStatus.FAILED

    current_time[0] += timedelta(minutes=1)
    retry = repository.claim_execution(action.action_id, action.idempotency_key)
    assert retry.outcome is ExecutionClaimOutcome.RETRY_CLAIMED
    assert retry.attempt_number == 2

    current_time[0] += timedelta(minutes=1)
    succeeded = repository.complete_execution(action.action_id, action.idempotency_key)
    assert succeeded.status is MailboxActionStatus.SUCCEEDED
    transition_count = len(succeeded.transition_history)

    current_time[0] += timedelta(minutes=1)
    duplicate = repository.claim_execution(action.action_id, action.idempotency_key)
    assert duplicate.outcome is ExecutionClaimOutcome.ALREADY_SUCCEEDED
    assert duplicate.should_execute is False
    assert len(duplicate.action.transition_history) == transition_count


def test_active_execution_rejects_duplicate_claim(tmp_path: Path) -> None:
    repository, action, _ = approved_repository(tmp_path)
    assert action.idempotency_key is not None
    repository.claim_execution(action.action_id, action.idempotency_key)

    with pytest.raises(ActionExecutionInProgressError, match="already in progress"):
        repository.claim_execution(action.action_id, action.idempotency_key)


def test_unknown_execution_outcome_requires_reconciliation_before_retry(tmp_path: Path) -> None:
    repository, action, _ = approved_repository(tmp_path)
    assert action.idempotency_key is not None
    repository.claim_execution(action.action_id, action.idempotency_key)
    repository.mark_write_in_flight(action.action_id, action.idempotency_key)

    unknown = repository.mark_execution_unknown(
        action.action_id,
        action.idempotency_key,
        note="PATCH response was not received",
    )

    assert unknown.status is MailboxActionStatus.OUTCOME_UNKNOWN
    with pytest.raises(ActionExecutionGuardError, match="cannot be executed"):
        repository.claim_execution(action.action_id, action.idempotency_key)


def test_write_in_flight_state_cannot_be_reclaimed_as_stale(tmp_path: Path) -> None:
    repository, action, current_time = approved_repository(tmp_path)
    assert action.idempotency_key is not None
    repository.claim_execution(action.action_id, action.idempotency_key)
    in_flight = repository.mark_write_in_flight(action.action_id, action.idempotency_key)
    assert in_flight.status is MailboxActionStatus.WRITE_IN_FLIGHT

    current_time[0] += timedelta(days=1)
    with pytest.raises(ActionExecutionGuardError, match="cannot be executed"):
        repository.claim_execution(
            action.action_id,
            action.idempotency_key,
            stale_after_seconds=0,
        )


def test_stale_execution_is_failed_then_reclaimed(tmp_path: Path) -> None:
    repository, action, current_time = approved_repository(tmp_path)
    assert action.idempotency_key is not None
    current_time[0] += timedelta(minutes=1)
    repository.claim_execution(action.action_id, action.idempotency_key)

    current_time[0] += timedelta(seconds=30)
    recovered = repository.claim_execution(
        action.action_id,
        action.idempotency_key,
        stale_after_seconds=10,
    )

    assert recovered.outcome is ExecutionClaimOutcome.RETRY_CLAIMED
    assert recovered.attempt_number == 2
    assert recovered.action.status is MailboxActionStatus.EXECUTING
    assert recovered.action.transition_history[-2].to_status is MailboxActionStatus.FAILED
    assert "lease expired" in (recovered.action.transition_history[-2].note or "").lower()


def test_execution_guard_rejects_wrong_key_and_unapproved_action(tmp_path: Path) -> None:
    action = make_action()
    repository = ActionQueueRepository(tmp_path / "queue.json", clock=lambda: CREATED_AT)
    repository.enqueue((action,))
    assert action.idempotency_key is not None

    with pytest.raises(ActionExecutionGuardError, match="cannot be executed"):
        repository.claim_execution(action.action_id, action.idempotency_key)

    repository.transition(
        action.action_id,
        MailboxActionStatus.APPROVED,
        actor=ActionActor.USER,
    )
    with pytest.raises(ActionExecutionGuardError, match="does not match"):
        repository.claim_execution(action.action_id, "0" * 64)


def test_action_rejects_idempotency_key_that_does_not_match_content() -> None:
    action = make_action()
    payload = action.model_dump()
    payload["idempotency_key"] = "0" * 64

    with pytest.raises(ValidationError, match="does not match the mailbox mutation"):
        MailboxAction.model_validate(payload)


def test_non_stale_lease_and_negative_threshold_are_rejected(tmp_path: Path) -> None:
    repository, action, current_time = approved_repository(tmp_path)
    assert action.idempotency_key is not None
    repository.claim_execution(action.action_id, action.idempotency_key)

    current_time[0] += timedelta(seconds=5)
    with pytest.raises(ActionExecutionInProgressError, match="not stale"):
        repository.claim_execution(
            action.action_id,
            action.idempotency_key,
            stale_after_seconds=10,
        )
    with pytest.raises(ValueError, match="must not be negative"):
        repository.claim_execution(
            action.action_id,
            action.idempotency_key,
            stale_after_seconds=-1,
        )
