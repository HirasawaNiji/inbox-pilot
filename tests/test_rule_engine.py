"""Tests for YAML-driven feature extraction and explainable scoring."""

import json
from pathlib import Path

import pytest

from inbox_agent.loader import load_dataset
from inbox_agent.models import Priority
from inbox_agent.normalizer import normalize_message
from inbox_agent.rule_engine import (
    RuleEngine,
    RulePolicyNotFoundError,
    RulePolicyValidationError,
    RulePolicyYAMLError,
    load_policy,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rules.yaml"
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
EXPECTED_PATH = ROOT / "data" / "eval" / "expected_results.json"


def load_messages_by_id() -> dict[str, object]:
    dataset = load_dataset(DATASET_PATH)
    return {message.source_id: normalize_message(message) for message in dataset.messages}


def test_load_policy_reads_valid_yaml() -> None:
    policy = load_policy(POLICY_PATH)

    assert policy.policy_version == "rules-v1"
    assert policy.base_score == 30
    assert policy.thresholds.p1 == 80
    assert "student@example.edu" in policy.user_context.mailbox_addresses


def test_load_policy_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RulePolicyNotFoundError, match="does not exist"):
        load_policy(tmp_path / "missing.yaml")


def test_load_policy_reports_invalid_yaml(tmp_path: Path) -> None:
    policy_path = tmp_path / "rules.yaml"
    policy_path.write_text("weights: [broken", encoding="utf-8")

    with pytest.raises(RulePolicyYAMLError, match="invalid YAML"):
        load_policy(policy_path)


def test_load_policy_rejects_invalid_threshold_order(tmp_path: Path) -> None:
    raw_policy = POLICY_PATH.read_text(encoding="utf-8")
    invalid_policy = raw_policy.replace("P1: 80\n  P2: 50", "P1: 40\n  P2: 50")
    policy_path = tmp_path / "rules.yaml"
    policy_path.write_text(invalid_policy, encoding="utf-8")

    with pytest.raises(RulePolicyValidationError, match="InboxPilot schema"):
        load_policy(policy_path)


def test_course_registration_uses_deadline_not_opening_date() -> None:
    messages = load_messages_by_id()
    engine = RuleEngine.from_yaml(POLICY_PATH)

    features = engine.extract_features(messages["sample-001-course-registration"])

    assert len(features.detected_dates) == 1
    assert features.detected_dates[0].day == 21


def test_bulk_exam_change_remains_high_priority() -> None:
    messages = load_messages_by_id()
    engine = RuleEngine.from_yaml(POLICY_PATH)

    result = engine.evaluate(messages["sample-005-exam-room-change"])
    reason_codes = {reason.code for reason in result.reasons}

    assert result.suggested_priority is Priority.P1
    assert "bulk_mail" in reason_codes
    assert "urgent_keyword" in reason_codes


def test_high_importance_promotion_stays_low_priority() -> None:
    messages = load_messages_by_id()
    engine = RuleEngine.from_yaml(POLICY_PATH)

    result = engine.evaluate(messages["sample-013-high-importance-promotion"])
    reason_codes = {reason.code for reason in result.reasons}

    assert result.suggested_priority is Priority.P5
    assert "high_importance" in reason_codes
    assert "unsubscribe" in reason_codes
    assert "external_sender" in reason_codes


def test_negated_action_does_not_increase_priority() -> None:
    messages = load_messages_by_id()
    engine = RuleEngine.from_yaml(POLICY_PATH)

    features = engine.extract_features(messages["sample-003-course-reading"])
    result = engine.evaluate(messages["sample-003-course-reading"])

    assert features.action_keywords == ()
    assert features.no_action_keywords == ("无需提交",)
    assert result.suggested_priority is Priority.P3


def test_unsubscribe_does_not_trigger_course_cancellation_rule() -> None:
    messages = load_messages_by_id()
    engine = RuleEngine.from_yaml(POLICY_PATH)

    features = engine.extract_features(messages["sample-012-campus-newsletter"])
    result = engine.evaluate(messages["sample-012-campus-newsletter"])

    assert features.urgent_keywords == ()
    assert features.contains_unsubscribe is True
    assert result.suggested_priority is Priority.P5


@pytest.mark.parametrize(
    "source_id",
    [
        "sample-015-activity-registration-deadline",
        "sample-019-empty-subject-administrative",
        "sample-020-career-fair-registration",
    ],
)
def test_ambiguous_samples_require_review(source_id: str) -> None:
    messages = load_messages_by_id()
    engine = RuleEngine.from_yaml(POLICY_PATH)

    assert engine.evaluate(messages[source_id]).requires_review is True


def test_all_sample_priorities_match_human_labels() -> None:
    messages = load_messages_by_id()
    expected_data = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    expected_priorities = {
        label["source_id"]: label["expected_priority"] for label in expected_data["labels"]
    }
    engine = RuleEngine.from_yaml(POLICY_PATH)

    actual_priorities = {
        source_id: engine.evaluate(message).suggested_priority.value
        for source_id, message in messages.items()
    }

    assert actual_priorities == expected_priorities


def test_every_score_has_valid_explainable_arithmetic() -> None:
    messages = load_messages_by_id()
    engine = RuleEngine.from_yaml(POLICY_PATH)

    for message in messages.values():
        result = engine.evaluate(message)
        calculated = result.base_score + sum(reason.score_change for reason in result.reasons)
        assert result.final_score == max(0, min(100, calculated))
        assert result.reasons
