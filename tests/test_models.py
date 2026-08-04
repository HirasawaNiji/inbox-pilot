"""Tests for InboxPilot's core data contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from inbox_agent.models import (
    AttachmentMetadata,
    BodyType,
    EmailAddress,
    EmailMessage,
    MailSource,
    MessageBody,
    MessageDataset,
    MessageFeatures,
    Priority,
    RuleEvaluation,
    ScoreReason,
    TriageResult,
    UserContext,
)


def make_message(source_id: str = "sample-001") -> EmailMessage:
    """Build a minimal valid message for focused tests."""

    return EmailMessage(
        source=MailSource.MOCK,
        source_id=source_id,
        subject="课程通知",
        from_address=EmailAddress(name="张老师", address="teacher@example.edu"),
        to_recipients=(EmailAddress(address="student@example.edu"),),
        received_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
        body=MessageBody(content_type=BodyType.TEXT, content="请查看课程通知。"),
    )


@pytest.mark.parametrize(
    "address",
    [
        "missing-at.example.edu",
        "two@@example.edu",
        "@example.edu",
        "student@",
        "student @example.edu",
        "student@.example.edu",
    ],
)
def test_email_address_rejects_malformed_values(address: str) -> None:
    with pytest.raises(ValidationError):
        EmailAddress(address=address)


def test_email_address_strips_outer_whitespace() -> None:
    email = EmailAddress(name="  张老师  ", address="  teacher@example.edu  ")

    assert email.name == "张老师"
    assert email.address == "teacher@example.edu"


def test_message_requires_timezone_aware_received_at() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        EmailMessage(
            source=MailSource.MOCK,
            source_id="sample-001",
            from_address=EmailAddress(address="teacher@example.edu"),
            received_at=datetime(2026, 8, 4, 9, 30),
            body=MessageBody(content_type=BodyType.TEXT, content="通知"),
        )


def test_message_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EmailMessage.model_validate(
            {
                "source": "mock",
                "source_id": "sample-001",
                "from_address": {"address": "teacher@example.edu"},
                "received_at": "2026-08-04T09:30:00+08:00",
                "body": {"content_type": "text", "content": "通知"},
                "importence": "high",
            }
        )


def test_message_rejects_invalid_importance() -> None:
    payload = make_message().model_dump()
    payload["importance"] = "urgent"

    with pytest.raises(ValidationError):
        EmailMessage.model_validate(payload)


def test_message_is_immutable() -> None:
    message = make_message()

    with pytest.raises(ValidationError, match="frozen_instance"):
        message.subject = "被意外修改的标题"


def test_effective_sender_falls_back_to_from_address() -> None:
    message = make_message()

    assert message.effective_sender == message.from_address


def test_attachment_metadata_sets_effective_attachment_flag() -> None:
    message = make_message().model_copy(
        update={
            "attachments": (
                AttachmentMetadata(
                    name="assignment.pdf",
                    content_type="application/pdf",
                    size_bytes=1024,
                ),
            )
        }
    )

    assert message.has_attachments is False
    assert message.effective_has_attachments is True


def test_dataset_rejects_duplicate_provider_identity() -> None:
    message = make_message()

    with pytest.raises(ValidationError, match="duplicate"):
        MessageDataset(messages=(message, message))


def test_dataset_accepts_same_id_from_different_sources() -> None:
    mock_message = make_message()
    graph_message = mock_message.model_copy(update={"source": MailSource.MICROSOFT_GRAPH})

    dataset = MessageDataset(messages=(mock_message, graph_message))

    assert len(dataset.messages) == 2


def test_user_context_normalizes_addresses_and_domains() -> None:
    context = UserContext(
        mailbox_addresses=frozenset({"STUDENT@EXAMPLE.EDU"}),
        trusted_senders=frozenset({"Teacher@Example.EDU"}),
        trusted_domains=frozenset({"@Example.EDU"}),
    )

    assert context.mailbox_addresses == frozenset({"student@example.edu"})
    assert context.trusted_senders == frozenset({"teacher@example.edu"})
    assert context.trusted_domains == frozenset({"example.edu"})


def test_features_reject_naive_detected_date() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        MessageFeatures(detected_dates=(datetime(2026, 8, 5, 17, 0),))


def test_rule_evaluation_clamps_score_to_one_hundred() -> None:
    evaluation = RuleEvaluation(
        base_score=80,
        final_score=100,
        suggested_priority=Priority.P1,
        reasons=(
            ScoreReason(
                code="trusted_sender",
                description="发件人在可信名单中",
                score_change=30,
            ),
        ),
    )

    assert evaluation.final_score == 100


def test_rule_evaluation_rejects_inconsistent_score() -> None:
    with pytest.raises(ValidationError, match="final_score"):
        RuleEvaluation(
            base_score=30,
            final_score=90,
            suggested_priority=Priority.P1,
            reasons=(
                ScoreReason(
                    code="trusted_sender",
                    description="发件人在可信名单中",
                    score_change=30,
                ),
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("score", -1),
        ("score", 101),
        ("confidence", -0.1),
        ("confidence", 1.1),
        ("priority", "urgent"),
        ("category", "Academic Deadline"),
    ],
)
def test_triage_result_rejects_invalid_boundaries(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "message_id": "sample-001",
        "priority": Priority.P1,
        "score": 85,
        "confidence": 0.9,
        "category": "academic_deadline",
        "summary": "课程作业即将截止",
        "evaluated_at": datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
        "policy_version": "rules-v1",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        TriageResult.model_validate(payload)


def test_triage_result_requires_aware_deadline() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        TriageResult(
            message_id="sample-001",
            priority=Priority.P1,
            score=85,
            confidence=0.9,
            category="academic_deadline",
            summary="课程作业即将截止",
            deadline=datetime(2026, 8, 5, 17, 0),
            evaluated_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
            policy_version="rules-v1",
        )


def test_triage_result_serializes_enums_as_json_values() -> None:
    result = TriageResult(
        message_id="sample-001",
        priority=Priority.P1,
        score=85,
        confidence=0.9,
        category="academic_deadline",
        summary="课程作业即将截止",
        evaluated_at=datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
        policy_version="rules-v1",
    )

    serialized = result.model_dump(mode="json")

    assert serialized["priority"] == "P1"
    assert serialized["decision_source"] == "rule"
