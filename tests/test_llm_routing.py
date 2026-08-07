"""Tests for explainable selective LLM routing."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from inbox_agent.llm import (
    LLMRouter,
    LLMRoutingDecision,
    LLMRoutingMode,
    LLMRoutingPolicyNotFoundError,
    LLMRoutingPolicyValidationError,
    LLMRoutingPolicyYAMLError,
    LLMRoutingReasonCode,
    load_llm_routing_policy,
)
from inbox_agent.models import MessageFeatures, Priority, TriageResult

ROOT = Path(__file__).resolve().parents[1]
ROUTING_PATH = ROOT / "config" / "llm_routing.yaml"
EVALUATED_AT = datetime(2026, 8, 8, 18, 0, tzinfo=UTC)
SHANGHAI = timezone(timedelta(hours=8))


def make_result(
    *,
    confidence: float = 0.9,
    requires_review: bool = False,
) -> TriageResult:
    """Build one rule result suitable for routing tests."""

    return TriageResult(
        message_id="routing-sample",
        priority=Priority.P3,
        score=45,
        confidence=confidence,
        category="general_notice",
        summary="用于路由测试的规则摘要。",
        requires_review=requires_review,
        evaluated_at=EVALUATED_AT,
        policy_version="rules-v1",
    )


def reason_codes(decision: LLMRoutingDecision) -> set[LLMRoutingReasonCode]:
    """Return reason codes from one immutable routing decision."""

    return {reason.code for reason in decision.reasons}


def test_load_llm_routing_policy_reads_selective_defaults() -> None:
    policy = load_llm_routing_policy(ROUTING_PATH)

    assert policy.routing_version == "llm-routing-v1"
    assert policy.mode is LLMRoutingMode.SELECTIVE
    assert policy.confidence_threshold == 0.75
    assert policy.route_on_multiple_deadlines is True


def test_load_llm_routing_policy_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(LLMRoutingPolicyNotFoundError, match="does not exist"):
        load_llm_routing_policy(tmp_path / "missing.yaml")


def test_load_llm_routing_policy_reports_invalid_yaml(tmp_path: Path) -> None:
    policy_path = tmp_path / "routing.yaml"
    policy_path.write_text("mode: [broken", encoding="utf-8")

    with pytest.raises(LLMRoutingPolicyYAMLError, match="invalid YAML"):
        load_llm_routing_policy(policy_path)


def test_load_llm_routing_policy_rejects_invalid_threshold(tmp_path: Path) -> None:
    raw_policy = ROUTING_PATH.read_text(encoding="utf-8")
    policy_path = tmp_path / "routing.yaml"
    policy_path.write_text(
        raw_policy.replace("confidence_threshold: 0.75", "confidence_threshold: 1.5"),
        encoding="utf-8",
    )

    with pytest.raises(LLMRoutingPolicyValidationError, match="InboxPilot schema"):
        load_llm_routing_policy(policy_path)


def test_high_confidence_result_without_conflict_skips_provider() -> None:
    decision = LLMRouter.from_yaml(ROUTING_PATH).decide(make_result(), MessageFeatures())

    assert decision.should_analyze is False
    assert reason_codes(decision) == {LLMRoutingReasonCode.HIGH_CONFIDENCE_RULE}


def test_low_rule_confidence_routes_to_provider() -> None:
    decision = LLMRouter().decide(make_result(confidence=0.6), MessageFeatures())

    assert decision.should_analyze is True
    assert LLMRoutingReasonCode.LOW_RULE_CONFIDENCE in reason_codes(decision)


def test_rule_review_flag_routes_to_provider() -> None:
    decision = LLMRouter().decide(make_result(requires_review=True), MessageFeatures())

    assert decision.should_analyze is True
    assert reason_codes(decision) == {LLMRoutingReasonCode.RULE_REQUIRES_REVIEW}


@pytest.mark.parametrize(
    ("features", "expected_code"),
    [
        (
            MessageFeatures(contains_deadline_language=True),
            LLMRoutingReasonCode.AMBIGUOUS_DEADLINE,
        ),
        (
            MessageFeatures(
                contains_deadline_language=True,
                detected_dates=(
                    datetime(2026, 8, 10, 12, 0, tzinfo=SHANGHAI),
                    datetime(2026, 8, 11, 17, 0, tzinfo=SHANGHAI),
                ),
            ),
            LLMRoutingReasonCode.MULTIPLE_DEADLINES,
        ),
        (
            MessageFeatures(
                action_keywords=("报名",),
                no_action_keywords=("自愿参加",),
            ),
            LLMRoutingReasonCode.ACTION_NO_ACTION_CONFLICT,
        ),
        (
            MessageFeatures(
                provider_marked_high_importance=True,
                bulk_keywords=("推广",),
                contains_unsubscribe=True,
            ),
            LLMRoutingReasonCode.IMPORTANCE_CONTENT_CONFLICT,
        ),
        (
            MessageFeatures(
                urgent_keywords=("紧急",),
                no_action_keywords=("无需回复",),
            ),
            LLMRoutingReasonCode.URGENT_NO_ACTION_CONFLICT,
        ),
    ],
)
def test_conflicting_signals_route_to_provider(
    features: MessageFeatures,
    expected_code: LLMRoutingReasonCode,
) -> None:
    decision = LLMRouter().decide(make_result(), features)

    assert decision.should_analyze is True
    assert expected_code in reason_codes(decision)


def test_full_evaluation_mode_always_routes() -> None:
    decision = LLMRouter.analyze_all().decide(make_result(), MessageFeatures())

    assert decision.should_analyze is True
    assert reason_codes(decision) == {LLMRoutingReasonCode.FULL_EVALUATION}
