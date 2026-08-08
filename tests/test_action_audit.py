"""Tests for privacy-bounded append-only action audit logging."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from inbox_agent.actions import (
    ActionActor,
    ActionAuditEventType,
    ActionAuditLog,
    ActionAuditStorageError,
    ActionEvidence,
    ActionQueue,
    AuditGraphOperation,
    AuditGraphOutcome,
    CategoryWritePlan,
    MailboxAction,
    MailboxActionStatus,
    OutlookCategorySnapshot,
    audit_event_for_graph_operation,
    audit_events_for_action,
    audit_events_for_dry_run,
    build_dry_run,
)
from inbox_agent.models import Priority, RuleEvaluation, TriageResult

CREATED_AT = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)
RAW_MESSAGE_ID = "raw-outlook-message-id-001"


def make_action(*, approved: bool = False) -> MailboxAction:
    rule = RuleEvaluation(
        base_score=90,
        final_score=90,
        suggested_priority=Priority.P1,
        requires_review=True,
    )
    result = TriageResult(
        message_id=RAW_MESSAGE_ID,
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
        action_id="action-audit-example-001",
        message_id=RAW_MESSAGE_ID,
        current_snapshot=OutlookCategorySnapshot(
            categories=("School", "InboxPilot/P3"),
            observed_at=CREATED_AT - timedelta(minutes=2),
            change_key="change-key-001",
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
    if not approved:
        return action
    return action.transition(
        MailboxActionStatus.APPROVED,
        occurred_at=CREATED_AT + timedelta(minutes=1),
        actor=ActionActor.USER,
        note="人工确认分类正确。",
    )


def test_action_events_hash_message_identity_and_capture_transition() -> None:
    action = make_action(approved=True)

    events = audit_events_for_action(action)
    serialized = json.dumps(
        [event.model_dump(mode="json") for event in events],
        ensure_ascii=False,
    )

    assert [event.event_type for event in events] == [
        ActionAuditEventType.ACTION_GENERATED,
        ActionAuditEventType.ACTION_STATUS_CHANGED,
    ]
    assert events[1].from_status is MailboxActionStatus.PENDING_REVIEW
    assert events[1].to_status is MailboxActionStatus.APPROVED
    assert len(events[0].message_id_sha256) == 64
    assert RAW_MESSAGE_ID not in serialized
    assert "body" not in events[0].model_dump()


def test_audit_log_appends_jsonl_and_skips_deterministic_duplicates(tmp_path: Path) -> None:
    log_path = tmp_path / "data/private/audit/actions.jsonl"
    audit_log = ActionAuditLog(log_path)
    generated = audit_events_for_action(make_action())

    first = audit_log.append_unique(generated)
    content_after_first = log_path.read_bytes()
    second = audit_log.append_unique(generated)

    assert first.appended_count == 1
    assert second.appended_count == 0
    assert second.skipped_count == 1
    assert log_path.read_bytes() == content_after_first
    assert audit_log.load() == generated
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 1


def test_audit_log_backfills_new_transition_without_rewriting_generation(
    tmp_path: Path,
) -> None:
    audit_log = ActionAuditLog(tmp_path / "actions.jsonl")
    audit_log.append_unique(audit_events_for_action(make_action()))

    report = audit_log.append_unique(audit_events_for_action(make_action(approved=True)))
    loaded = audit_log.load()

    assert report.appended_count == 1
    assert report.skipped_count == 1
    assert len(loaded) == 2
    assert loaded[-1].action_status is MailboxActionStatus.APPROVED


def test_dry_run_event_records_zero_graph_writes_and_category_counts() -> None:
    action = make_action(approved=True)
    queue = ActionQueue(
        updated_at=CREATED_AT + timedelta(minutes=1),
        actions=(action,),
    )
    report = build_dry_run(
        queue,
        "queue.json",
        generated_at=CREATED_AT + timedelta(minutes=2),
    )

    events = audit_events_for_dry_run(queue.actions, report)
    event = events[0]

    assert event.event_type is ActionAuditEventType.DRY_RUN_PLANNED
    assert event.dry_run is not None
    assert event.dry_run.graph_write_request_count == 0
    assert event.dry_run.add_categories == (
        "InboxPilot/P1",
        "InboxPilot/security_alert",
        "InboxPilot/review",
    )
    assert event.dry_run.remove_categories == ("InboxPilot/P3",)


def test_graph_operation_event_records_counts_without_raw_message_identity() -> None:
    action = make_action(approved=True)
    event = audit_event_for_graph_operation(
        action,
        occurred_at=CREATED_AT + timedelta(minutes=2),
        operation=AuditGraphOperation.EXECUTE,
        outcome=AuditGraphOutcome.CONFLICT,
        attempt_number=1,
        graph_read_request_count=1,
        graph_write_request_count=0,
        note="Preflight conflict",
    )

    assert event.event_type is ActionAuditEventType.GRAPH_OPERATION_RECORDED
    assert event.graph_operation is not None
    assert event.graph_operation.outcome is AuditGraphOutcome.CONFLICT
    assert event.graph_operation.graph_write_request_count == 0
    assert RAW_MESSAGE_ID not in event.model_dump_json()


@pytest.mark.parametrize("content", ["not-json\n", "\n"])
def test_audit_log_rejects_invalid_or_blank_lines(tmp_path: Path, content: str) -> None:
    log_path = tmp_path / "actions.jsonl"
    log_path.write_text(content, encoding="utf-8")

    with pytest.raises(ActionAuditStorageError):
        ActionAuditLog(log_path).load()
