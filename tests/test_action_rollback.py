"""Tests for controlled local rollback planning without Graph writes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from inbox_agent.actions import (
    ActionActor,
    ActionAuditEventType,
    ActionQueue,
    MailboxAction,
    MailboxActionStatus,
    RollbackDryRunPlan,
    RollbackPlanError,
    audit_event_for_rollback_dry_run,
    build_action_idempotency_key,
    build_review_actions,
    build_rollback_dry_run,
)
from inbox_agent.loader import load_dataset
from inbox_agent.pipeline import OfflinePipeline

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rules.yaml"
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
CREATED_AT = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)
ORIGINAL_CATEGORIES = (
    "School",
    "Important",
    "InboxPilot/P5",
    "InboxPilot/old_notice",
)


def make_action(*, succeeded: bool = True, with_idempotency_key: bool = True) -> MailboxAction:
    dataset = load_dataset(DATASET_PATH)
    dataset = dataset.model_copy(update={"messages": dataset.messages[:1]})
    analysis = OfflinePipeline.from_yaml(POLICY_PATH).analyze_dataset(
        dataset,
        evaluated_at=CREATED_AT,
    )
    base = build_review_actions(dataset, analysis)[0]
    payload = base.model_dump()
    payload["current_snapshot"]["categories"] = ORIGINAL_CATEGORIES
    payload["current_snapshot"]["change_key"] = "original-change-key"
    payload["idempotency_key"] = (
        build_action_idempotency_key(
            message_id=base.message_id,
            action_type=base.action_type,
            current_categories=ORIGINAL_CATEGORIES,
            change_key="original-change-key",
            managed_categories=base.write_plan.managed_categories,
            policy_version=base.evidence.triage_result.policy_version,
        )
        if with_idempotency_key
        else None
    )
    action = MailboxAction.model_validate(payload)
    if not succeeded:
        return action
    approved = action.transition(
        MailboxActionStatus.APPROVED,
        occurred_at=CREATED_AT + timedelta(minutes=1),
        actor=ActionActor.USER,
    )
    executing = approved.transition(
        MailboxActionStatus.EXECUTING,
        occurred_at=CREATED_AT + timedelta(minutes=2),
        actor=ActionActor.SYSTEM,
    )
    return executing.transition(
        MailboxActionStatus.SUCCEEDED,
        occurred_at=CREATED_AT + timedelta(minutes=3),
        actor=ActionActor.SYSTEM,
    )


def test_rollback_restores_original_managed_and_preserves_user_categories() -> None:
    action = make_action()
    queue = ActionQueue(updated_at=action.updated_at, actions=(action,))

    report = build_rollback_dry_run(
        queue,
        action.action_id,
        "queue.json",
        reason="The classification was incorrect",
        generated_at=CREATED_AT + timedelta(minutes=4),
    )
    plan = report.plan

    assert plan.source_status is MailboxActionStatus.SUCCEEDED
    assert plan.expected_current_categories == (
        "School",
        "Important",
        *action.write_plan.managed_categories,
    )
    assert plan.restore_managed_categories == (
        "InboxPilot/P5",
        "InboxPilot/old_notice",
    )
    assert plan.add_categories == plan.restore_managed_categories
    assert set(plan.remove_categories) == set(action.write_plan.managed_categories)
    assert plan.final_categories == ORIGINAL_CATEGORIES
    assert plan.preserve_unmanaged_categories is True
    assert plan.would_write is True
    assert report.graph_write_request_count == 0


def test_rollback_key_is_stable_across_reason_and_time() -> None:
    action = make_action()
    queue = ActionQueue(updated_at=action.updated_at, actions=(action,))

    first = build_rollback_dry_run(
        queue,
        action.action_id,
        "queue.json",
        reason="First explanation",
        generated_at=CREATED_AT + timedelta(minutes=4),
    )
    second = build_rollback_dry_run(
        queue,
        action.action_id,
        "queue.json",
        reason="More detailed explanation",
        generated_at=CREATED_AT + timedelta(minutes=5),
    )

    assert first.plan.rollback_idempotency_key == second.plan.rollback_idempotency_key
    assert len(first.plan.rollback_idempotency_key) == 64


def test_rollback_requires_success_key_and_nonempty_reason() -> None:
    pending = make_action(succeeded=False)
    pending_queue = ActionQueue(updated_at=pending.updated_at, actions=(pending,))
    with pytest.raises(RollbackPlanError, match="requires a succeeded action"):
        build_rollback_dry_run(
            pending_queue,
            pending.action_id,
            "queue.json",
            reason="Incorrect result",
        )

    without_key = make_action(with_idempotency_key=False)
    keyless_queue = ActionQueue(updated_at=without_key.updated_at, actions=(without_key,))
    with pytest.raises(RollbackPlanError, match="no idempotency key"):
        build_rollback_dry_run(
            keyless_queue,
            without_key.action_id,
            "queue.json",
            reason="Incorrect result",
        )

    action = make_action()
    queue = ActionQueue(updated_at=action.updated_at, actions=(action,))
    with pytest.raises(RollbackPlanError, match="must not be empty"):
        build_rollback_dry_run(queue, action.action_id, "queue.json", reason="   ")
    with pytest.raises(RollbackPlanError, match="does not exist"):
        build_rollback_dry_run(queue, "action-missing", "queue.json", reason="Incorrect")


def test_rollback_plan_rejects_tampered_diff_and_idempotency_key() -> None:
    action = make_action()
    queue = ActionQueue(updated_at=action.updated_at, actions=(action,))
    plan = build_rollback_dry_run(
        queue,
        action.action_id,
        "queue.json",
        reason="Incorrect result",
    ).plan

    diff_payload = plan.model_dump()
    diff_payload["remove_categories"] = ()
    with pytest.raises(ValidationError, match="remove_categories"):
        RollbackDryRunPlan.model_validate(diff_payload)

    key_payload = plan.model_dump()
    key_payload["rollback_idempotency_key"] = "0" * 64
    with pytest.raises(ValidationError, match="idempotency key"):
        RollbackDryRunPlan.model_validate(key_payload)

    user_category_payload = plan.model_dump()
    user_category_payload["expected_current_categories"] = (
        *plan.expected_current_categories,
        "Personal",
    )
    with pytest.raises(ValidationError, match="exactly the original user categories"):
        RollbackDryRunPlan.model_validate(user_category_payload)


def test_rollback_audit_event_is_private_and_does_not_change_status() -> None:
    action = make_action()
    queue = ActionQueue(updated_at=action.updated_at, actions=(action,))
    report = build_rollback_dry_run(
        queue,
        action.action_id,
        "queue.json",
        reason="Incorrect result",
        generated_at=CREATED_AT + timedelta(minutes=4),
    )

    event = audit_event_for_rollback_dry_run(action, report)
    serialized = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)

    assert event.event_type is ActionAuditEventType.ROLLBACK_DRY_RUN_PLANNED
    assert event.actor is ActionActor.USER
    assert event.action_status is MailboxActionStatus.SUCCEEDED
    assert event.from_status is None
    assert event.to_status is None
    assert event.note == "Incorrect result"
    assert event.dry_run is not None
    assert event.dry_run.graph_write_request_count == 0
    assert action.message_id not in serialized
