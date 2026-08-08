"""Tests for InboxPilot's core data contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from inbox_agent.models import (
    ActionItem,
    AttachmentMetadata,
    BodyType,
    DeadlineKind,
    DecisionSource,
    EmailAddress,
    EmailMessage,
    ExtractedDeadline,
    LLMAnalysisResult,
    LLMMessageAnalysis,
    LLMTokenUsage,
    MailSource,
    MessageBody,
    MessageCategory,
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


def make_llm_analysis() -> LLMMessageAnalysis:
    """Build a representative structured LLM response."""

    deadline = ExtractedDeadline(
        value=datetime(2026, 8, 8, 20, 0, tzinfo=UTC),
        kind=DeadlineKind.EXPLICIT,
        confidence=0.98,
        evidence="请在 2026 年 8 月 8 日 20:00 前补交",
    )
    return LLMMessageAnalysis(
        priority=Priority.P1,
        category="academic_deadline",
        summary="课程项目报告需要补交修订版。",
        action_items=(
            ActionItem(
                description="补交课程项目报告修订版",
                confidence=0.96,
                evidence="请补交修订版",
                deadline=deadline,
            ),
        ),
        deadline=deadline,
        confidence=0.95,
        rationale="邮件来自课程教师，并包含明确补交要求和截止时间。",
        requires_review=False,
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


def test_extracted_deadline_requires_timezone() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        ExtractedDeadline(
            value=datetime(2026, 8, 8, 20, 0),
            kind=DeadlineKind.EXPLICIT,
            confidence=0.9,
            evidence="8 月 8 日 20:00 前",
        )


def test_action_item_supports_structured_deadline_evidence() -> None:
    analysis = make_llm_analysis()
    action_item = analysis.action_items[0]

    assert action_item.description == "补交课程项目报告修订版"
    assert action_item.deadline == analysis.deadline
    assert action_item.deadline is not None
    assert action_item.deadline.kind is DeadlineKind.EXPLICIT


def test_llm_analysis_rejects_duplicate_action_items() -> None:
    duplicate = ActionItem(
        description="提交申请",
        confidence=0.8,
        evidence=None,
        deadline=None,
    )

    with pytest.raises(ValidationError, match="duplicate action item"):
        LLMMessageAnalysis(
            priority=Priority.P2,
            category="administrative_deadline",
            summary="申请即将截止。",
            action_items=(
                duplicate,
                duplicate.model_copy(update={"description": "提交申请"}),
            ),
            deadline=None,
            confidence=0.8,
            rationale="邮件包含申请要求。",
            requires_review=False,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("priority", "urgent"),
        ("category", "Academic Deadline"),
        ("confidence", -0.1),
        ("confidence", 1.1),
        ("summary", ""),
        ("rationale", ""),
    ],
)
def test_llm_analysis_rejects_invalid_output(field: str, value: object) -> None:
    payload = make_llm_analysis().model_dump()
    payload[field] = value

    with pytest.raises(ValidationError):
        LLMMessageAnalysis.model_validate(payload)


def test_llm_analysis_exposes_strict_json_schema() -> None:
    schema = LLMMessageAnalysis.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "priority",
        "category",
        "summary",
        "action_items",
        "deadline",
        "confidence",
        "rationale",
        "requires_review",
    }
    assert set(schema["$defs"]["ActionItem"]["required"]) == {
        "description",
        "confidence",
        "evidence",
        "deadline",
    }

    payload = make_llm_analysis().model_dump()
    payload["unexpected_reasoning"] = "must be rejected"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LLMMessageAnalysis.model_validate(payload)


def test_llm_token_usage_validates_cached_subset() -> None:
    usage = LLMTokenUsage(
        input_tokens=1_000,
        output_tokens=200,
        cached_input_tokens=800,
    )

    assert usage.total_tokens == 1_200

    with pytest.raises(ValidationError, match="must not exceed"):
        LLMTokenUsage(
            input_tokens=100,
            output_tokens=20,
            cached_input_tokens=101,
        )


def test_llm_analysis_result_requires_traceable_metadata() -> None:
    result = LLMAnalysisResult(
        message_id="sample-002-assignment-deadline",
        analysis=make_llm_analysis(),
        provider="openai",
        model_name="gpt-5-mini",
        prompt_version="triage-v4",
        analyzed_at=datetime(2026, 8, 7, 14, 30, tzinfo=UTC),
        duration_ms=850,
        usage=LLMTokenUsage(input_tokens=900, output_tokens=180),
        request_id="request-example-001",
    )

    serialized = result.model_dump(mode="json")

    assert serialized["schema_version"] == "1.0"
    assert serialized["analysis"]["priority"] == "P1"
    assert serialized["analysis"]["category"] == "academic_deadline"
    assert serialized["analysis"]["deadline"]["kind"] == "explicit"
    assert serialized["usage"]["input_tokens"] == 900


def test_llm_analysis_result_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        LLMAnalysisResult(
            message_id="sample-001",
            analysis=make_llm_analysis(),
            provider="openai",
            model_name="gpt-5-mini",
            prompt_version="triage-v4",
            analyzed_at=datetime(2026, 8, 7, 14, 30),
            duration_ms=500,
        )


def test_triage_result_accepts_structured_action_items() -> None:
    analysis = make_llm_analysis()
    result = TriageResult(
        message_id="sample-002-assignment-deadline",
        priority=analysis.priority,
        score=90,
        confidence=analysis.confidence,
        category=analysis.category,
        summary=analysis.summary,
        action_items=analysis.action_items,
        deadline=analysis.deadline.value if analysis.deadline else None,
        requires_review=analysis.requires_review,
        decision_source=DecisionSource.LLM,
        evaluated_at=datetime(2026, 8, 7, 14, 30, tzinfo=UTC),
        policy_version="llm-triage-v1",
    )

    assert result.action_items[0].deadline == analysis.deadline
    assert analysis.category is MessageCategory.ACADEMIC_DEADLINE
    assert result.decision_source is DecisionSource.LLM


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
