"""Tests for Stage 3 mailbox action contracts and state transitions."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from inbox_agent.actions import (
    ActionActor,
    ActionEvidence,
    CategoryWritePlan,
    MailboxAction,
    MailboxActionStatus,
    OutlookCategorySnapshot,
    can_transition,
)
from inbox_agent.models import (
    DecisionSource,
    LLMAnalysisResult,
    LLMMessageAnalysis,
    Priority,
    RuleEvaluation,
    TriageResult,
)

CREATED_AT = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)


def make_rule_evaluation(priority: Priority = Priority.P1) -> RuleEvaluation:
    return RuleEvaluation(
        base_score=90,
        final_score=90,
        suggested_priority=priority,
        requires_review=True,
    )


def make_triage_result(
    *,
    message_id: str = "graph-message-001",
    priority: Priority = Priority.P1,
    decision_source: DecisionSource = DecisionSource.RULE,
    requires_review: bool = True,
) -> TriageResult:
    return TriageResult(
        message_id=message_id,
        priority=priority,
        score=90,
        confidence=0.95,
        category="security_alert",
        summary="账号安全设置需要确认。",
        requires_review=requires_review,
        decision_source=decision_source,
        evaluated_at=CREATED_AT - timedelta(minutes=2),
        policy_version="rules-v1",
    )


def make_llm_result(message_id: str = "graph-message-001") -> LLMAnalysisResult:
    return LLMAnalysisResult(
        message_id=message_id,
        analysis=LLMMessageAnalysis(
            priority=Priority.P1,
            category="security_alert",
            summary="账号安全设置需要确认。",
            action_items=(),
            deadline=None,
            confidence=0.95,
            rationale="邮件包含账户安全行动要求。",
            requires_review=True,
        ),
        provider="deepseek",
        model_name="deepseek-v4-flash",
        prompt_version="triage-v4",
        analyzed_at=CREATED_AT - timedelta(minutes=1),
        duration_ms=500,
    )


def make_action(
    *,
    decision_source: DecisionSource = DecisionSource.RULE,
    requires_review: bool = True,
) -> MailboxAction:
    triage_result = make_triage_result(
        decision_source=decision_source,
        requires_review=requires_review,
    )
    categories = ["InboxPilot/P1", "InboxPilot/security_alert"]
    if requires_review:
        categories.append("InboxPilot/review")
    return MailboxAction(
        action_id="action-example-001",
        message_id=triage_result.message_id,
        current_snapshot=OutlookCategorySnapshot(
            categories=("School", "Important"),
            observed_at=CREATED_AT - timedelta(minutes=3),
            change_key="change-key-001",
        ),
        write_plan=CategoryWritePlan(managed_categories=tuple(categories)),
        evidence=ActionEvidence(
            rule_evaluation=make_rule_evaluation(),
            llm_analysis=(
                make_llm_result() if decision_source is not DecisionSource.RULE else None
            ),
            triage_result=triage_result,
        ),
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def test_new_action_is_strict_immutable_and_pending_review() -> None:
    action = make_action()

    assert action.status is MailboxActionStatus.PENDING_REVIEW
    assert action.write_plan.preserve_unmanaged_categories is True
    assert action.current_snapshot.categories == ("School", "Important")
    assert action.transition_history == ()

    with pytest.raises(ValidationError, match="frozen_instance"):
        action.status = MailboxActionStatus.APPROVED


def test_category_plan_rejects_unmanaged_or_duplicate_categories() -> None:
    with pytest.raises(ValidationError, match="InboxPilot/ prefix"):
        CategoryWritePlan(managed_categories=("InboxPilot/P1", "School"))

    with pytest.raises(ValidationError, match="unique ignoring case"):
        CategoryWritePlan(managed_categories=("InboxPilot/P1", "inboxpilot/p1"))


def test_action_plan_must_match_final_triage_result() -> None:
    action = make_action()
    payload = action.model_dump()
    payload["write_plan"] = {
        "managed_categories": ("InboxPilot/P2", "InboxPilot/security_alert"),
        "preserve_unmanaged_categories": True,
    }

    with pytest.raises(ValidationError, match="must match the final triage result"):
        MailboxAction.model_validate(payload)


def test_action_requires_review_category_only_when_needed() -> None:
    without_review = make_action(requires_review=False)

    assert without_review.write_plan.managed_categories == (
        "InboxPilot/P1",
        "InboxPilot/security_alert",
    )

    payload = without_review.model_dump()
    payload["write_plan"]["managed_categories"] = (
        "InboxPilot/P1",
        "InboxPilot/security_alert",
        "InboxPilot/review",
    )
    with pytest.raises(ValidationError, match="must match the final triage result"):
        MailboxAction.model_validate(payload)


def test_hybrid_action_requires_matching_llm_evidence() -> None:
    action = make_action(decision_source=DecisionSource.HYBRID)

    assert action.evidence.llm_analysis is not None

    payload = action.model_dump()
    payload["evidence"]["llm_analysis"] = None
    with pytest.raises(ValidationError, match="require an LLM analysis"):
        MailboxAction.model_validate(payload)


def test_action_rejects_mismatched_message_identity() -> None:
    action = make_action()
    payload = action.model_dump()
    payload["message_id"] = "another-message"

    with pytest.raises(ValidationError, match="message IDs must match"):
        MailboxAction.model_validate(payload)


def test_action_requires_timezone_aware_timestamps() -> None:
    action = make_action()
    payload = action.model_dump()
    payload["created_at"] = datetime(2026, 8, 8, 9, 0)

    with pytest.raises(ValidationError, match="timezone"):
        MailboxAction.model_validate(payload)


def test_happy_path_transitions_preserve_complete_history() -> None:
    action = make_action()
    approved_at = CREATED_AT + timedelta(minutes=1)
    executing_at = CREATED_AT + timedelta(minutes=2)
    succeeded_at = CREATED_AT + timedelta(minutes=3)

    approved = action.transition(
        MailboxActionStatus.APPROVED,
        occurred_at=approved_at,
        actor=ActionActor.USER,
    )
    executing = approved.transition(
        MailboxActionStatus.EXECUTING,
        occurred_at=executing_at,
        actor=ActionActor.SYSTEM,
    )
    succeeded = executing.transition(
        MailboxActionStatus.SUCCEEDED,
        occurred_at=succeeded_at,
        actor=ActionActor.SYSTEM,
    )

    assert action.status is MailboxActionStatus.PENDING_REVIEW
    assert succeeded.status is MailboxActionStatus.SUCCEEDED
    assert succeeded.updated_at == succeeded_at
    assert [event.to_status for event in succeeded.transition_history] == [
        MailboxActionStatus.APPROVED,
        MailboxActionStatus.EXECUTING,
        MailboxActionStatus.SUCCEEDED,
    ]


def test_user_can_reject_but_system_cannot_approve() -> None:
    action = make_action()

    rejected = action.transition(
        MailboxActionStatus.REJECTED,
        occurred_at=CREATED_AT + timedelta(minutes=1),
        actor=ActionActor.USER,
        note="用户决定不修改此邮件。",
    )
    assert rejected.status is MailboxActionStatus.REJECTED

    with pytest.raises(ValidationError, match="requires actor=user"):
        action.transition(
            MailboxActionStatus.APPROVED,
            occurred_at=CREATED_AT + timedelta(minutes=1),
            actor=ActionActor.SYSTEM,
        )


def test_invalid_state_jump_is_rejected() -> None:
    action = make_action()

    assert can_transition(
        MailboxActionStatus.PENDING_REVIEW,
        MailboxActionStatus.APPROVED,
    )
    assert not can_transition(
        MailboxActionStatus.PENDING_REVIEW,
        MailboxActionStatus.EXECUTING,
    )

    with pytest.raises(ValidationError, match="invalid action transition"):
        action.transition(
            MailboxActionStatus.EXECUTING,
            occurred_at=CREATED_AT + timedelta(minutes=1),
            actor=ActionActor.SYSTEM,
        )


def test_failed_action_can_retry_and_requires_failure_note() -> None:
    action = make_action()
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

    with pytest.raises(ValidationError, match="requires a note"):
        executing.transition(
            MailboxActionStatus.FAILED,
            occurred_at=CREATED_AT + timedelta(minutes=3),
            actor=ActionActor.SYSTEM,
        )

    failed = executing.transition(
        MailboxActionStatus.FAILED,
        occurred_at=CREATED_AT + timedelta(minutes=3),
        actor=ActionActor.SYSTEM,
        note="Graph 请求超时。",
    )
    retried = failed.transition(
        MailboxActionStatus.EXECUTING,
        occurred_at=CREATED_AT + timedelta(minutes=4),
        actor=ActionActor.SYSTEM,
    )

    assert retried.status is MailboxActionStatus.EXECUTING


def test_rollback_completion_requires_system_and_reason() -> None:
    action = make_action()
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
    succeeded = executing.transition(
        MailboxActionStatus.SUCCEEDED,
        occurred_at=CREATED_AT + timedelta(minutes=3),
        actor=ActionActor.SYSTEM,
    )
    with pytest.raises(ValidationError, match="requires actor=system"):
        succeeded.transition(
            MailboxActionStatus.ROLLED_BACK,
            occurred_at=CREATED_AT + timedelta(minutes=4),
            actor=ActionActor.USER,
            note="用户要求撤销 InboxPilot 分类。",
        )
    rolled_back = succeeded.transition(
        MailboxActionStatus.ROLLED_BACK,
        occurred_at=CREATED_AT + timedelta(minutes=4),
        actor=ActionActor.SYSTEM,
        note="用户要求撤销 InboxPilot 分类。",
    )

    assert rolled_back.status is MailboxActionStatus.ROLLED_BACK


def test_tampered_transition_history_is_rejected() -> None:
    action = make_action()
    approved = action.transition(
        MailboxActionStatus.APPROVED,
        occurred_at=CREATED_AT + timedelta(minutes=1),
        actor=ActionActor.USER,
    )
    payload = approved.model_dump()
    payload["status"] = MailboxActionStatus.SUCCEEDED

    with pytest.raises(ValidationError, match="status must match"):
        MailboxAction.model_validate(payload)
