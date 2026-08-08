"""Tests for private action queue construction, persistence, and review updates."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from inbox_agent.actions import (
    ActionActor,
    ActionEvidence,
    ActionQueue,
    ActionQueueConflictError,
    ActionQueueNotFoundError,
    ActionQueueRepository,
    ActionQueueStorageError,
    CategoryWritePlan,
    MailboxAction,
    MailboxActionStatus,
    OutlookCategorySnapshot,
    build_review_actions,
)
from inbox_agent.loader import load_dataset
from inbox_agent.models import Priority, RuleEvaluation, TriageResult
from inbox_agent.pipeline import OfflinePipeline

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rules.yaml"
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
CREATED_AT = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)


def make_action(
    *,
    action_id: str = "action-queue-example-001",
    message_id: str = "message-001",
) -> MailboxAction:
    rule = RuleEvaluation(
        base_score=90,
        final_score=90,
        suggested_priority=Priority.P1,
        requires_review=True,
    )
    result = TriageResult(
        message_id=message_id,
        priority=Priority.P1,
        score=90,
        confidence=0.9,
        category="security_alert",
        summary="请确认账号安全设置。",
        requires_review=True,
        evaluated_at=CREATED_AT - timedelta(minutes=1),
        policy_version="rules-v1",
    )
    return MailboxAction(
        action_id=action_id,
        message_id=message_id,
        current_snapshot=OutlookCategorySnapshot(
            categories=("School",),
            observed_at=CREATED_AT - timedelta(minutes=2),
        ),
        write_plan=CategoryWritePlan(
            managed_categories=(
                "InboxPilot/P1",
                "InboxPilot/security_alert",
                "InboxPilot/review",
            )
        ),
        evidence=ActionEvidence(rule_evaluation=rule, triage_result=result),
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def test_missing_queue_loads_as_empty_without_creating_file(tmp_path: Path) -> None:
    queue_path = tmp_path / "data/private/action_queue.json"
    repository = ActionQueueRepository(queue_path, clock=lambda: CREATED_AT)

    queue = repository.load()

    assert queue.actions == ()
    assert queue.pending_count == 0
    assert not queue_path.exists()


def test_queue_round_trip_uses_strict_json_schema(tmp_path: Path) -> None:
    queue_path = tmp_path / "action_queue.json"
    repository = ActionQueueRepository(queue_path, clock=lambda: CREATED_AT)
    report = repository.enqueue((make_action(),))

    loaded = repository.load()
    payload = json.loads(queue_path.read_text(encoding="utf-8"))

    assert report.added_count == 1
    assert report.pending_count == 1
    assert loaded.actions[0].action_id == "action-queue-example-001"
    assert payload["schema_version"] == "1.0"
    assert payload["actions"][0]["status"] == "pending_review"


def test_queue_rejects_invalid_json_and_duplicate_ids(tmp_path: Path) -> None:
    queue_path = tmp_path / "action_queue.json"
    queue_path.write_text("not-json", encoding="utf-8")
    repository = ActionQueueRepository(queue_path, clock=lambda: CREATED_AT)

    with pytest.raises(ActionQueueStorageError, match="does not match"):
        repository.load()

    with pytest.raises(ValueError, match="duplicate action IDs"):
        ActionQueue(
            updated_at=CREATED_AT,
            actions=(make_action(), make_action()),
        )


def test_enqueue_skips_same_proposal_even_after_review(tmp_path: Path) -> None:
    queue_path = tmp_path / "action_queue.json"
    review_time = CREATED_AT + timedelta(minutes=1)
    repository = ActionQueueRepository(queue_path, clock=lambda: review_time)
    action = make_action()
    repository.enqueue((action,))
    repository.transition(
        action.action_id,
        MailboxActionStatus.APPROVED,
        actor=ActionActor.USER,
    )

    rebuilt = action.model_copy(
        update={
            "created_at": CREATED_AT + timedelta(minutes=2),
            "updated_at": CREATED_AT + timedelta(minutes=2),
        }
    )
    report = repository.enqueue((rebuilt,))

    assert report.added_count == 0
    assert report.skipped_count == 1
    assert repository.load().actions[0].status is MailboxActionStatus.APPROVED


def test_enqueue_rejects_action_id_collision(tmp_path: Path) -> None:
    repository = ActionQueueRepository(tmp_path / "queue.json", clock=lambda: CREATED_AT)
    repository.enqueue((make_action(),))

    with pytest.raises(ActionQueueConflictError, match="different content"):
        repository.enqueue((make_action(message_id="message-002"),))


def test_repository_approves_and_rejects_actions(tmp_path: Path) -> None:
    times = iter(
        (
            CREATED_AT,
            CREATED_AT + timedelta(minutes=1),
            CREATED_AT + timedelta(minutes=2),
            CREATED_AT + timedelta(minutes=3),
        )
    )
    repository = ActionQueueRepository(tmp_path / "queue.json", clock=lambda: next(times))
    first = make_action()
    second = make_action(action_id="action-queue-example-002", message_id="message-002")
    repository.enqueue((first, second))

    approved = repository.transition(
        first.action_id,
        MailboxActionStatus.APPROVED,
        actor=ActionActor.USER,
        note="分类建议正确。",
    )
    rejected = repository.transition(
        second.action_id,
        MailboxActionStatus.REJECTED,
        actor=ActionActor.USER,
        note="不希望修改此邮件。",
    )

    assert approved.status is MailboxActionStatus.APPROVED
    assert rejected.status is MailboxActionStatus.REJECTED
    assert repository.load().pending_count == 0


def test_repository_reports_missing_action(tmp_path: Path) -> None:
    repository = ActionQueueRepository(tmp_path / "queue.json", clock=lambda: CREATED_AT)

    with pytest.raises(ActionQueueNotFoundError, match="does not exist"):
        repository.transition(
            "action-missing",
            MailboxActionStatus.APPROVED,
            actor=ActionActor.USER,
        )


def test_builder_creates_stable_actions_from_pipeline_evidence() -> None:
    dataset = load_dataset(DATASET_PATH)
    dataset = dataset.model_copy(update={"messages": dataset.messages[:2]})
    pipeline = OfflinePipeline.from_yaml(POLICY_PATH)
    first_report = pipeline.analyze_dataset(dataset, evaluated_at=CREATED_AT)
    second_report = pipeline.analyze_dataset(
        dataset,
        evaluated_at=CREATED_AT + timedelta(minutes=5),
    )

    first_actions = build_review_actions(dataset, first_report)
    second_actions = build_review_actions(dataset, second_report)

    assert len(first_actions) == 2
    assert [action.action_id for action in first_actions] == [
        action.action_id for action in second_actions
    ]
    assert first_actions[0].status is MailboxActionStatus.PENDING_REVIEW
    assert len(first_actions[0].idempotency_key or "") == 64
    assert [action.idempotency_key for action in first_actions] == [
        action.idempotency_key for action in second_actions
    ]
    assert first_actions[0].evidence.rule_evaluation in first_report.rule_evaluations
    assert all(
        category.startswith("InboxPilot/")
        for action in first_actions
        for category in action.write_plan.managed_categories
    )
