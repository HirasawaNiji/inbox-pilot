"""Tests for conservative rule and LLM result fusion."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from inbox_agent.llm import (
    LLMFusionDecision,
    LLMFusionEngine,
    LLMFusionMode,
    LLMFusionPolicy,
    LLMFusionPolicyNotFoundError,
    LLMFusionPolicyValidationError,
    LLMFusionPolicyYAMLError,
    LLMFusionReasonCode,
    load_llm_fusion_policy,
)
from inbox_agent.models import (
    ActionItem,
    DeadlineKind,
    DecisionSource,
    ExtractedDeadline,
    LLMAnalysisResult,
    LLMMessageAnalysis,
    Priority,
    TriageResult,
)

ROOT = Path(__file__).resolve().parents[1]
FUSION_PATH = ROOT / "config" / "llm_fusion.yaml"
EVALUATED_AT = datetime(2026, 8, 8, 18, 0, tzinfo=UTC)
SHANGHAI = timezone(timedelta(hours=8))
RULE_DEADLINE = datetime(2026, 8, 10, 18, 0, tzinfo=SHANGHAI)


def make_rule_result(
    *,
    priority: Priority = Priority.P3,
    deadline: datetime | None = None,
    requires_review: bool = False,
) -> TriageResult:
    """Build one deterministic rule result."""

    return TriageResult(
        message_id="fusion-sample",
        priority=priority,
        score=45,
        confidence=0.9 if not requires_review else 0.6,
        category="general_notice",
        summary="规则摘要。",
        deadline=deadline,
        requires_review=requires_review,
        evaluated_at=EVALUATED_AT,
        policy_version="rules-v1",
    )


def make_llm_result(
    *,
    priority: Priority = Priority.P3,
    confidence: float = 0.9,
    deadline: datetime | None = RULE_DEADLINE,
    requires_review: bool = False,
    message_id: str = "fusion-sample",
) -> LLMAnalysisResult:
    """Build one traceable structured LLM result."""

    extracted_deadline = (
        None
        if deadline is None
        else ExtractedDeadline(
            value=deadline,
            kind=DeadlineKind.EXPLICIT,
            confidence=0.95,
            evidence="明确截止时间",
        )
    )
    analysis = LLMMessageAnalysis(
        priority=priority,
        category="academic_deadline",
        summary="LLM 提取的课程任务摘要。",
        action_items=(
            ActionItem(
                description="提交课程任务",
                confidence=0.95,
                evidence="请提交课程任务",
                deadline=extracted_deadline,
            ),
        ),
        deadline=extracted_deadline,
        confidence=confidence,
        rationale="存在课程任务。",
        requires_review=requires_review,
    )
    return LLMAnalysisResult(
        message_id=message_id,
        analysis=analysis,
        provider="fake",
        model_name="fake-structured-v1",
        prompt_version="triage-v1",
        analyzed_at=EVALUATED_AT,
        duration_ms=1,
    )


def fusion_reason_codes(decision: LLMFusionDecision) -> set[LLMFusionReasonCode]:
    """Return stable reason codes from one fusion decision."""

    return {reason.code for reason in decision.reasons}


def test_load_llm_fusion_policy_reads_conservative_defaults() -> None:
    policy = load_llm_fusion_policy(FUSION_PATH)

    assert policy.fusion_version == "llm-fusion-v1"
    assert policy.mode is LLMFusionMode.CONSERVATIVE
    assert policy.minimum_llm_confidence == 0.8
    assert policy.allow_priority_downgrade is False


def test_load_llm_fusion_policy_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(LLMFusionPolicyNotFoundError, match="does not exist"):
        load_llm_fusion_policy(tmp_path / "missing.yaml")


def test_load_llm_fusion_policy_reports_invalid_yaml(tmp_path: Path) -> None:
    policy_path = tmp_path / "fusion.yaml"
    policy_path.write_text("mode: [broken", encoding="utf-8")

    with pytest.raises(LLMFusionPolicyYAMLError, match="invalid YAML"):
        load_llm_fusion_policy(policy_path)


def test_load_llm_fusion_policy_rejects_invalid_confidence(tmp_path: Path) -> None:
    raw_policy = FUSION_PATH.read_text(encoding="utf-8")
    policy_path = tmp_path / "fusion.yaml"
    policy_path.write_text(
        raw_policy.replace("minimum_llm_confidence: 0.80", "minimum_llm_confidence: 2"),
        encoding="utf-8",
    )

    with pytest.raises(LLMFusionPolicyValidationError, match="InboxPilot schema"):
        load_llm_fusion_policy(policy_path)


def test_agreement_adopts_structured_fields_without_forcing_review() -> None:
    fused, decision = LLMFusionEngine.from_yaml(FUSION_PATH).fuse(
        make_rule_result(),
        make_llm_result(),
    )

    assert fused.priority is Priority.P3
    assert fused.category == "academic_deadline"
    assert fused.summary == "LLM 提取的课程任务摘要。"
    assert len(fused.action_items) == 1
    assert fused.deadline == RULE_DEADLINE
    assert fused.requires_review is False
    assert fused.decision_source is DecisionSource.HYBRID
    assert decision.applied is True
    assert fusion_reason_codes(decision) == {
        LLMFusionReasonCode.STRUCTURED_FIELDS_ADOPTED,
        LLMFusionReasonCode.PRIORITY_AGREEMENT,
        LLMFusionReasonCode.DEADLINE_ADDED,
    }


def test_high_confidence_llm_can_upgrade_but_forces_review() -> None:
    rule = make_rule_result(priority=Priority.P3)
    fused, decision = LLMFusionEngine().fuse(
        rule,
        make_llm_result(priority=Priority.P1),
    )

    assert fused.priority is Priority.P1
    assert fused.score == rule.score
    assert fused.requires_review is True
    assert LLMFusionReasonCode.LLM_PRIORITY_UPGRADE in fusion_reason_codes(decision)
    assert LLMFusionReasonCode.PRIORITY_DISAGREEMENT_REVIEW in fusion_reason_codes(decision)


def test_llm_priority_downgrade_is_blocked_by_default() -> None:
    fused, decision = LLMFusionEngine().fuse(
        make_rule_result(priority=Priority.P1),
        make_llm_result(priority=Priority.P4),
    )

    assert fused.priority is Priority.P1
    assert fused.requires_review is True
    assert LLMFusionReasonCode.LLM_PRIORITY_DOWNGRADE_BLOCKED in fusion_reason_codes(decision)


def test_policy_can_explicitly_allow_priority_downgrade() -> None:
    engine = LLMFusionEngine(LLMFusionPolicy(allow_priority_downgrade=True))

    fused, decision = engine.fuse(
        make_rule_result(priority=Priority.P1),
        make_llm_result(priority=Priority.P4),
    )

    assert fused.priority is Priority.P4
    assert fused.requires_review is True
    assert LLMFusionReasonCode.LLM_PRIORITY_DOWNGRADE in fusion_reason_codes(decision)


def test_low_confidence_llm_keeps_rule_fields_and_forces_review() -> None:
    rule = make_rule_result()
    fused, decision = LLMFusionEngine().fuse(
        rule,
        make_llm_result(priority=Priority.P1, confidence=0.6),
    )

    assert fused.priority is rule.priority
    assert fused.category == rule.category
    assert fused.summary == rule.summary
    assert fused.action_items == rule.action_items
    assert fused.deadline == rule.deadline
    assert fused.requires_review is True
    assert fused.confidence == 0.6
    assert fusion_reason_codes(decision) == {LLMFusionReasonCode.LLM_BELOW_CONFIDENCE}


def test_llm_review_request_always_propagates() -> None:
    fused, decision = LLMFusionEngine().fuse(
        make_rule_result(),
        make_llm_result(requires_review=True),
    )

    assert fused.requires_review is True
    assert LLMFusionReasonCode.LLM_REVIEW_REQUESTED in fusion_reason_codes(decision)


def test_deadline_conflict_uses_earlier_time_and_forces_review() -> None:
    later_llm_deadline = datetime(2026, 8, 11, 18, 0, tzinfo=SHANGHAI)
    fused, decision = LLMFusionEngine().fuse(
        make_rule_result(deadline=RULE_DEADLINE),
        make_llm_result(deadline=later_llm_deadline),
    )

    assert fused.deadline == RULE_DEADLINE
    assert fused.requires_review is True
    assert LLMFusionReasonCode.DEADLINE_CONFLICT in fusion_reason_codes(decision)


def test_matching_deadlines_do_not_create_conflict() -> None:
    fused, decision = LLMFusionEngine().fuse(
        make_rule_result(deadline=RULE_DEADLINE),
        make_llm_result(deadline=RULE_DEADLINE),
    )

    assert fused.deadline == RULE_DEADLINE
    assert fused.requires_review is False
    assert LLMFusionReasonCode.DEADLINE_AGREEMENT in fusion_reason_codes(decision)


def test_sidecar_only_mode_returns_original_rule_result() -> None:
    rule = make_rule_result()
    fused, decision = LLMFusionEngine.sidecar_only().fuse(rule, make_llm_result())

    assert fused == rule
    assert decision.applied is False
    assert fusion_reason_codes(decision) == {LLMFusionReasonCode.SIDECAR_ONLY}


def test_fusion_rejects_different_message_ids() -> None:
    with pytest.raises(ValueError, match="different message IDs"):
        LLMFusionEngine().fuse(
            make_rule_result(),
            make_llm_result(message_id="different-message"),
        )
