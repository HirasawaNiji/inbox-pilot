"""Integration tests for the deterministic offline analysis pipeline."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inbox_agent.llm import (
    FakeLLMProvider,
    LLMFusionDecision,
    LLMFusionEngine,
    LLMProvider,
    LLMRouter,
    LLMRoutingDecision,
)
from inbox_agent.loader import load_dataset
from inbox_agent.models import (
    DecisionSource,
    LLMAnalysisResult,
    LLMMessageAnalysis,
    MessageCategory,
    MessageDataset,
    MessageFeatures,
    NormalizedMessage,
    Priority,
    RuleEvaluation,
    TriageResult,
)
from inbox_agent.pipeline import OfflinePipeline, analyze_file
from inbox_agent.rule_engine import RuleEngine

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rules.yaml"
ROUTING_PATH = ROOT / "config" / "llm_routing.yaml"
FUSION_PATH = ROOT / "config" / "llm_fusion.yaml"
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
EXPECTED_PATH = ROOT / "data" / "eval" / "expected_results.json"
EVALUATED_AT = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)


def expected_labels() -> dict[str, dict[str, object]]:
    payload = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    return {label["source_id"]: label for label in payload["labels"]}


def make_llm_analysis(
    *,
    priority: Priority = Priority.P5,
    category: MessageCategory = MessageCategory.GENERAL_NOTICE,
) -> LLMMessageAnalysis:
    """Build a strict sidecar result intentionally independent of rules."""

    return LLMMessageAnalysis(
        priority=priority,
        category=category,
        summary="Fake Provider 的离线旁路结果。",
        action_items=(),
        deadline=None,
        confidence=0.75,
        rationale="用于验证 Pipeline 接线，不作为最终决策。",
        requires_review=False,
    )


def first_messages(count: int = 2) -> MessageDataset:
    """Return a small valid dataset without duplicating sample fixtures."""

    dataset = load_dataset(DATASET_PATH)
    return dataset.model_copy(update={"messages": dataset.messages[:count]})


def test_pipeline_analyzes_complete_sample_dataset() -> None:
    report = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)

    assert report.processed_count == 50
    assert report.failure_count == 0
    assert report.review_count == 7
    assert report.schema_version == "1.0"
    assert report.policy_version == "rules-v1"
    assert report.evaluated_at == EVALUATED_AT
    assert report.llm_analysis_count == 0
    assert report.llm_failure_count == 0


def test_pipeline_priorities_and_categories_match_human_labels() -> None:
    labels = expected_labels()
    report = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)

    for result in report.results:
        expected = labels[result.message_id]
        assert result.priority.value == expected["expected_priority"]
        assert result.category == expected["expected_category"]
        assert result.requires_review is expected["requires_review"]


def test_rule_categories_are_supported_by_llm_taxonomy() -> None:
    report = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)
    supported_categories = {category.value for category in MessageCategory}

    assert {result.category for result in report.results} <= supported_categories


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

    assert report.processed_count == 49
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
    assert serialized["llm_analyses"] == []
    assert serialized["llm_failures"] == []
    assert serialized["llm_routing_decisions"] == []
    assert serialized["llm_fusion_decisions"] == []


def test_pipeline_collects_llm_sidecar_without_replacing_rule_decision() -> None:
    dataset = first_messages()
    responses = {message.source_id: make_llm_analysis() for message in dataset.messages}
    provider = FakeLLMProvider(responses, clock=lambda: EVALUATED_AT)
    pipeline = OfflinePipeline.from_yaml(
        POLICY_PATH,
        llm_provider=provider,
        llm_router=LLMRouter.analyze_all(),
        llm_fusion=LLMFusionEngine.sidecar_only(),
    )

    report = pipeline.analyze_dataset(dataset, evaluated_at=EVALUATED_AT)

    assert report.processed_count == 2
    assert report.failure_count == 0
    assert report.llm_analysis_count == 2
    assert report.llm_failure_count == 0
    assert report.llm_routed_count == 2
    assert report.llm_skipped_count == 0
    assert report.llm_fused_count == 0
    assert report.llm_sidecar_only_count == 2
    assert all(result.decision_source is DecisionSource.RULE for result in report.results)
    assert all(analysis.analysis.priority is Priority.P5 for analysis in report.llm_analyses)
    assert [analysis.message_id for analysis in report.llm_analyses] == [
        result.message_id for result in report.results
    ]
    assert provider.calls == tuple(message.source_id for message in dataset.messages)


def test_pipeline_selective_router_calls_provider_only_for_uncertain_messages() -> None:
    dataset = load_dataset(DATASET_PATH)
    responses = {message.source_id: make_llm_analysis() for message in dataset.messages}
    provider = FakeLLMProvider(responses, clock=lambda: EVALUATED_AT)
    pipeline = OfflinePipeline.from_yaml(
        POLICY_PATH,
        llm_provider=provider,
        llm_routing_path=ROUTING_PATH,
        llm_fusion_path=FUSION_PATH,
    )

    report = pipeline.analyze_dataset(dataset, evaluated_at=EVALUATED_AT)
    routed_ids = {
        decision.message_id for decision in report.llm_routing_decisions if decision.should_analyze
    }
    skipped_ids = {
        decision.message_id
        for decision in report.llm_routing_decisions
        if not decision.should_analyze
    }

    assert report.processed_count == 50
    assert report.failure_count == 0
    assert report.llm_routed_count == len(provider.calls)
    assert report.llm_analysis_count == report.llm_routed_count
    assert report.llm_fused_count == report.llm_analysis_count
    assert report.llm_sidecar_only_count == 0
    assert report.llm_skipped_count > 0
    assert routed_ids == set(provider.calls)
    assert "sample-015-activity-registration-deadline" in routed_ids
    assert "sample-019-empty-subject-administrative" in routed_ids
    assert "sample-001-course-registration" in skipped_ids
    assert "sample-002-assignment-deadline" in skipped_ids
    results = {result.message_id: result for result in report.results}
    assert results["sample-015-activity-registration-deadline"].decision_source is (
        DecisionSource.HYBRID
    )
    assert results["sample-001-course-registration"].decision_source is DecisionSource.RULE


def test_pipeline_rejects_router_and_routing_path_together() -> None:
    provider = FakeLLMProvider({})

    with pytest.raises(ValueError, match="either llm_router or llm_routing_path"):
        OfflinePipeline.from_yaml(
            POLICY_PATH,
            llm_provider=provider,
            llm_router=LLMRouter(),
            llm_routing_path=ROUTING_PATH,
        )


def test_pipeline_rejects_fusion_engine_and_path_together() -> None:
    provider = FakeLLMProvider({})

    with pytest.raises(ValueError, match="either llm_fusion or llm_fusion_path"):
        OfflinePipeline.from_yaml(
            POLICY_PATH,
            llm_provider=provider,
            llm_fusion=LLMFusionEngine(),
            llm_fusion_path=FUSION_PATH,
        )


class FailingLLMRouter(LLMRouter):
    """Router double that fails before any Provider call."""

    def decide(
        self,
        result: TriageResult,
        features: MessageFeatures,
    ) -> LLMRoutingDecision:
        raise RuntimeError("simulated routing failure")


def test_pipeline_isolates_routing_failure_from_rule_results() -> None:
    dataset = first_messages(1)
    message_id = dataset.messages[0].source_id
    provider = FakeLLMProvider(
        {message_id: make_llm_analysis()},
        clock=lambda: EVALUATED_AT,
    )
    pipeline = OfflinePipeline.from_yaml(
        POLICY_PATH,
        llm_provider=provider,
        llm_router=FailingLLMRouter(),
    )

    report = pipeline.analyze_dataset(dataset, evaluated_at=EVALUATED_AT)

    assert report.processed_count == 1
    assert report.failure_count == 0
    assert report.llm_routing_decisions == ()
    assert report.llm_analysis_count == 0
    assert report.llm_failure_count == 1
    assert report.llm_failures[0].stage == "llm_routing"
    assert report.llm_failures[0].error_message == "simulated routing failure"
    assert provider.calls == ()


class FailingLLMFusionEngine(LLMFusionEngine):
    """Fusion double that fails after a successful Provider response."""

    def fuse(
        self,
        rule_result: TriageResult,
        llm_result: LLMAnalysisResult,
    ) -> tuple[TriageResult, LLMFusionDecision]:
        raise RuntimeError("simulated fusion failure")


def test_pipeline_isolates_fusion_failure_and_keeps_rule_result() -> None:
    dataset = first_messages(1)
    message_id = dataset.messages[0].source_id
    provider = FakeLLMProvider(
        {message_id: make_llm_analysis()},
        clock=lambda: EVALUATED_AT,
    )
    pipeline = OfflinePipeline.from_yaml(
        POLICY_PATH,
        llm_provider=provider,
        llm_router=LLMRouter.analyze_all(),
        llm_fusion=FailingLLMFusionEngine(),
    )

    report = pipeline.analyze_dataset(dataset, evaluated_at=EVALUATED_AT)

    assert report.processed_count == 1
    assert report.results[0].decision_source is DecisionSource.RULE
    assert report.llm_analysis_count == 1
    assert report.llm_fusion_decisions == ()
    assert report.llm_failure_count == 1
    assert report.llm_failures[0].stage == "llm_fusion"
    assert report.llm_failures[0].error_message == "simulated fusion failure"


def test_pipeline_isolates_missing_llm_response_from_rule_results() -> None:
    dataset = first_messages()
    configured_id = dataset.messages[0].source_id
    provider = FakeLLMProvider(
        {configured_id: make_llm_analysis()},
        clock=lambda: EVALUATED_AT,
    )
    pipeline = OfflinePipeline.from_yaml(
        POLICY_PATH,
        llm_provider=provider,
        llm_router=LLMRouter.analyze_all(),
    )

    report = pipeline.analyze_dataset(dataset, evaluated_at=EVALUATED_AT)

    assert report.processed_count == 2
    assert report.failure_count == 0
    assert report.llm_analysis_count == 1
    assert report.llm_failure_count == 1
    assert report.llm_failures[0].stage == "llm_analysis"
    assert report.llm_failures[0].error_type == "LLMResponseNotConfiguredError"


def test_pipeline_can_stop_paid_llm_calls_after_first_failure() -> None:
    dataset = first_messages(2)
    pipeline = OfflinePipeline.from_yaml(
        POLICY_PATH,
        llm_provider=FakeLLMProvider({}),
        llm_router=LLMRouter.analyze_all(),
    )

    report = pipeline.analyze_dataset(
        dataset,
        evaluated_at=EVALUATED_AT,
        stop_on_llm_failure=True,
    )

    assert report.processed_count == 2
    assert report.llm_routed_count == 1
    assert report.llm_analysis_count == 0
    assert report.llm_failure_count == 1
    assert report.llm_failures[0].message_id == dataset.messages[0].source_id


class MismatchedLLMProvider:
    """Provider double that violates message identity while returning valid JSON."""

    provider_name = "mismatched_fake"
    model_name = "fake-structured-v1"
    prompt_version = "triage-v4"

    def analyze(self, message: NormalizedMessage) -> LLMAnalysisResult:
        return LLMAnalysisResult(
            message_id="different-message",
            analysis=make_llm_analysis(),
            provider=self.provider_name,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            analyzed_at=EVALUATED_AT,
            duration_ms=0,
        )


def test_pipeline_rejects_llm_result_for_different_message() -> None:
    provider: LLMProvider = MismatchedLLMProvider()
    pipeline = OfflinePipeline.from_yaml(
        POLICY_PATH,
        llm_provider=provider,
        llm_router=LLMRouter.analyze_all(),
    )

    report = pipeline.analyze_dataset(first_messages(1), evaluated_at=EVALUATED_AT)

    assert report.processed_count == 1
    assert report.llm_analysis_count == 0
    assert report.llm_failure_count == 1
    assert report.llm_failures[0].error_type == "LLMProviderResultMismatchError"
