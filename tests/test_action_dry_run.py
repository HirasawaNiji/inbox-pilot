"""Tests for pure local category dry-run planning."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from inbox_agent.actions import (
    ActionActor,
    ActionEvidence,
    ActionQueue,
    CategoryWritePlan,
    DryRunReport,
    MailboxAction,
    MailboxActionStatus,
    OutlookCategorySnapshot,
    build_dry_run,
)
from inbox_agent.models import Priority, RuleEvaluation, TriageResult

CREATED_AT = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)
MANAGED_CATEGORIES = (
    "InboxPilot/P1",
    "InboxPilot/security_alert",
    "InboxPilot/review",
)


def make_action(
    *,
    action_id: str = "action-dry-run-001",
    current_categories: tuple[str, ...] = ("School", "InboxPilot/P3"),
    approved: bool = True,
) -> MailboxAction:
    rule = RuleEvaluation(
        base_score=90,
        final_score=90,
        suggested_priority=Priority.P1,
        requires_review=True,
    )
    result = TriageResult(
        message_id=f"message-{action_id}",
        priority=Priority.P1,
        score=90,
        confidence=0.9,
        category="security_alert",
        summary="请确认账号安全设置。",
        requires_review=True,
        evaluated_at=CREATED_AT - timedelta(minutes=1),
        policy_version="rules-v1",
    )
    action = MailboxAction(
        action_id=action_id,
        message_id=result.message_id,
        current_snapshot=OutlookCategorySnapshot(
            categories=current_categories,
            observed_at=CREATED_AT - timedelta(minutes=2),
            change_key="change-key-001",
        ),
        write_plan=CategoryWritePlan(managed_categories=MANAGED_CATEGORIES),
        evidence=ActionEvidence(rule_evaluation=rule, triage_result=result),
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )
    if not approved:
        return action
    return action.transition(
        MailboxActionStatus.APPROVED,
        occurred_at=CREATED_AT + timedelta(minutes=1),
        actor=ActionActor.USER,
    )


def test_dry_run_preserves_user_categories_and_replaces_managed_categories() -> None:
    action = make_action(
        current_categories=("School", "Important", "InboxPilot/P3", "InboxPilot/newsletter")
    )
    queue = ActionQueue(updated_at=CREATED_AT + timedelta(minutes=1), actions=(action,))

    report = build_dry_run(
        queue,
        Path("data/private/action_queue.json"),
        generated_at=CREATED_AT + timedelta(minutes=2),
    )
    plan = report.plans[0]

    assert plan.add_categories == MANAGED_CATEGORIES
    assert plan.remove_categories == ("InboxPilot/P3", "InboxPilot/newsletter")
    assert plan.final_categories == ("School", "Important", *MANAGED_CATEGORIES)
    assert plan.preserve_unmanaged_categories is True
    assert plan.would_write is True
    assert report.graph_write_request_count == 0


def test_dry_run_skips_unapproved_actions() -> None:
    approved = make_action()
    pending = make_action(action_id="action-dry-run-002", approved=False)
    queue = ActionQueue(
        updated_at=CREATED_AT + timedelta(minutes=1),
        actions=(approved, pending),
    )

    report = build_dry_run(queue, "queue.json", generated_at=CREATED_AT + timedelta(minutes=2))

    assert report.queue_total_count == 2
    assert report.eligible_count == 1
    assert report.skipped_count == 1
    assert tuple(plan.action_id for plan in report.plans) == (approved.action_id,)


def test_dry_run_marks_already_matching_categories_as_no_change() -> None:
    action = make_action(current_categories=("School", *MANAGED_CATEGORIES))
    queue = ActionQueue(updated_at=CREATED_AT + timedelta(minutes=1), actions=(action,))

    report = build_dry_run(queue, "queue.json", generated_at=CREATED_AT + timedelta(minutes=2))
    plan = report.plans[0]

    assert plan.add_categories == ()
    assert plan.remove_categories == ()
    assert plan.would_write is False
    assert report.would_write_count == 0
    assert report.no_change_count == 1


def test_dry_run_report_rejects_nonzero_graph_writes_or_inconsistent_counts() -> None:
    with pytest.raises(ValidationError):
        DryRunReport(
            generated_at=CREATED_AT,
            queue_path=Path("queue.json"),
            queue_total_count=0,
            eligible_count=0,
            skipped_count=0,
            would_write_count=0,
            no_change_count=0,
            graph_write_request_count=1,
        )

    with pytest.raises(ValidationError, match="queue totals"):
        DryRunReport(
            generated_at=CREATED_AT,
            queue_path=Path("queue.json"),
            queue_total_count=2,
            eligible_count=0,
            skipped_count=1,
            would_write_count=0,
            no_change_count=0,
        )
