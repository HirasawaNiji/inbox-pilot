"""Integration tests for the deterministic offline analysis pipeline."""

import json
from datetime import UTC, datetime
from pathlib import Path

from inbox_agent.loader import load_dataset
from inbox_agent.models import DecisionSource, NormalizedMessage, RuleEvaluation
from inbox_agent.pipeline import OfflinePipeline, analyze_file
from inbox_agent.rule_engine import RuleEngine

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rules.yaml"
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
EXPECTED_PATH = ROOT / "data" / "eval" / "expected_results.json"
EVALUATED_AT = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)


def expected_labels() -> dict[str, dict[str, object]]:
    payload = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    return {label["source_id"]: label for label in payload["labels"]}


def test_pipeline_analyzes_complete_sample_dataset() -> None:
    report = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)

    assert report.processed_count == 20
    assert report.failure_count == 0
    assert report.review_count == 3
    assert report.schema_version == "1.0"
    assert report.policy_version == "rules-v1"
    assert report.evaluated_at == EVALUATED_AT


def test_pipeline_priorities_and_categories_match_human_labels() -> None:
    labels = expected_labels()
    report = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)

    for result in report.results:
        expected = labels[result.message_id]
        assert result.priority.value == expected["expected_priority"]
        assert result.category == expected["expected_category"]
        assert result.requires_review is expected["requires_review"]


def test_pipeline_results_are_sorted_by_priority_and_score() -> None:
    report = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)
    priority_order = {"P1": 0, "P2": 1, "P3": 2, "P4": 3, "P5": 4}
    sort_keys = [
        (priority_order[result.priority.value], -result.score) for result in report.results
    ]

    assert sort_keys == sorted(sort_keys)


def test_pipeline_builds_public_triage_fields() -> None:
    report = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)
    results = {result.message_id: result for result in report.results}
    assignment = results["sample-002-assignment-deadline"]
    incomplete = results["sample-019-empty-subject-administrative"]

    assert assignment.deadline is not None
    assert assignment.deadline.day == 8
    assert assignment.confidence == 0.9
    assert assignment.decision_source is DecisionSource.RULE
    assert assignment.policy_version == "rules-v1"
    assert assignment.reasons
    assert incomplete.summary == "请登录教务系统查看最新通知。"
    assert incomplete.confidence == 0.6


class FailingRuleEngine(RuleEngine):
    """Test double that fails for one known message only."""

    def evaluate(self, message: NormalizedMessage) -> RuleEvaluation:
        if message.source_id == "sample-010-tuition-payment":
            raise RuntimeError("simulated rule failure")
        return super().evaluate(message)


def test_pipeline_isolates_one_message_failure() -> None:
    dataset = load_dataset(DATASET_PATH)
    base_engine = RuleEngine.from_yaml(POLICY_PATH)
    pipeline = OfflinePipeline(FailingRuleEngine(base_engine.policy))

    report = pipeline.analyze_dataset(dataset, evaluated_at=EVALUATED_AT)

    assert report.processed_count == 19
    assert report.failure_count == 1
    assert report.failures[0].message_id == "sample-010-tuition-payment"
    assert report.failures[0].stage == "message_analysis"
    assert report.failures[0].error_type == "RuntimeError"
    assert report.failures[0].error_message == "simulated rule failure"


def test_pipeline_report_serializes_to_json_values() -> None:
    report = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)

    serialized = report.model_dump(mode="json")

    assert serialized["results"][0]["priority"] == "P1"
    assert serialized["results"][0]["decision_source"] == "rule"
    assert serialized["evaluated_at"] == "2026-08-07T18:00:00Z"
