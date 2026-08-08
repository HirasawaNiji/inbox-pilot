"""Tests for classification-focused real-provider validation metrics."""

from datetime import UTC, datetime

from inbox_agent.evaluation import ExpectedLabel, ExpectedResults
from inbox_agent.llm import validate_llm_classifications
from inbox_agent.models import (
    LLMAnalysisResult,
    LLMMessageAnalysis,
    LLMTokenUsage,
    MessageCategory,
    Priority,
)
from inbox_agent.pipeline import AnalysisFailure, AnalysisReport


def expected_results() -> ExpectedResults:
    return ExpectedResults(
        dataset_version="validation-v1",
        source_dataset="samples.json",
        labels=(
            ExpectedLabel(
                source_id="message-1",
                expected_priority=Priority.P1,
                expected_category="security_alert",
                requires_review=False,
                explanation="Security alert.",
            ),
            ExpectedLabel(
                source_id="message-2",
                expected_priority=Priority.P3,
                expected_category="general_notice",
                requires_review=True,
                explanation="Ambiguous notice.",
            ),
        ),
    )


def llm_result(
    message_id: str,
    priority: Priority,
    category: MessageCategory,
    requires_review: bool,
) -> LLMAnalysisResult:
    return LLMAnalysisResult(
        message_id=message_id,
        analysis=LLMMessageAnalysis(
            priority=priority,
            category=category,
            summary="Synthetic summary",
            action_items=(),
            deadline=None,
            confidence=0.9,
            rationale="Synthetic rationale",
            requires_review=requires_review,
        ),
        provider="deepseek",
        model_name="deepseek-v4-flash",
        prompt_version="triage-v4",
        analyzed_at=datetime(2026, 8, 8, tzinfo=UTC),
        duration_ms=125,
        usage=LLMTokenUsage(input_tokens=100, output_tokens=20, cached_input_tokens=10),
    )


def test_exact_full_coverage_passes_and_sums_usage() -> None:
    analysis = AnalysisReport(
        schema_version="1.0",
        policy_version="rules-v1",
        evaluated_at=datetime(2026, 8, 8, tzinfo=UTC),
        llm_analyses=(
            llm_result("message-1", Priority.P1, MessageCategory.SECURITY_ALERT, False),
            llm_result("message-2", Priority.P3, MessageCategory.GENERAL_NOTICE, True),
        ),
    )

    report = validate_llm_classifications(analysis, expected_results())

    assert report.passed is True
    assert report.provider == "deepseek"
    assert report.analyzed_count == 2
    assert report.priority_accuracy == 1.0
    assert report.tolerated_priority_accuracy == 1.0
    assert report.exact_match_accuracy == 1.0
    assert report.tolerated_exact_match_accuracy == 1.0
    assert report.input_tokens == 200
    assert report.output_tokens == 40
    assert report.cached_input_tokens == 20
    assert report.total_duration_ms == 250
    assert report.mismatches == ()


def test_missing_analysis_and_mismatch_fail_validation() -> None:
    analysis = AnalysisReport(
        schema_version="1.0",
        policy_version="rules-v1",
        evaluated_at=datetime(2026, 8, 8, tzinfo=UTC),
        llm_analyses=(llm_result("message-1", Priority.P5, MessageCategory.PROMOTION, True),),
        llm_failures=(
            AnalysisFailure(
                message_id="message-2",
                stage="llm_analysis",
                error_type="ProviderError",
                error_message="Synthetic failure",
            ),
        ),
    )

    report = validate_llm_classifications(analysis, expected_results())
    mismatch_fields = {mismatch.field for mismatch in report.mismatches}

    assert report.passed is False
    assert report.analyzed_count == 1
    assert report.provider_failure_count == 1
    assert report.failures[0].message_id == "message-2"
    assert report.failures[0].stage == "llm_analysis"
    assert report.failures[0].error_message == "Synthetic failure"
    assert report.priority_accuracy == 0.0
    assert report.tolerated_priority_accuracy == 0.0
    assert report.category_accuracy == 0.0
    assert report.review_accuracy == 0.0
    assert mismatch_fields == {"missing_analysis", "priority", "category", "requires_review"}


def test_explicit_priority_tolerance_passes_without_hiding_exact_variance() -> None:
    expected = ExpectedResults(
        dataset_version="validation-v1",
        source_dataset="samples.json",
        labels=(
            ExpectedLabel(
                source_id="message-1",
                expected_priority=Priority.P1,
                expected_category="campus_activity",
                requires_review=False,
                explanation="Rule baseline differs from the model policy.",
                llm_expected_priority=Priority.P2,
                llm_acceptable_priorities=(Priority.P3,),
                llm_expected_category="promotion",
            ),
        ),
    )
    analysis = AnalysisReport(
        schema_version="1.0",
        policy_version="rules-v1",
        evaluated_at=datetime(2026, 8, 8, tzinfo=UTC),
        llm_analyses=(llm_result("message-1", Priority.P3, MessageCategory.PROMOTION, False),),
    )

    report = validate_llm_classifications(analysis, expected)

    assert report.passed is True
    assert report.priority_accuracy == 0.0
    assert report.tolerated_priority_accuracy == 1.0
    assert report.exact_match_accuracy == 0.0
    assert report.tolerated_exact_match_accuracy == 1.0
    assert report.tolerances[0].expected == "P2"
    assert report.tolerances[0].actual == "P3"
    assert report.mismatches == ()


def test_provider_metadata_survives_when_every_analysis_fails() -> None:
    analysis = AnalysisReport(
        schema_version="1.0",
        policy_version="rules-v1",
        evaluated_at=datetime(2026, 8, 8, tzinfo=UTC),
        llm_failures=(
            AnalysisFailure(
                message_id="message-1",
                stage="llm_analysis",
                error_type="LLMProviderContractError",
                error_message="category: Field required",
            ),
        ),
    )

    report = validate_llm_classifications(
        analysis,
        expected_results(),
        provider_name="deepseek",
        model_name="deepseek-v4-flash",
        prompt_version="triage-v4",
    )

    assert report.provider == "deepseek"
    assert report.model_name == "deepseek-v4-flash"
    assert report.prompt_version == "triage-v4"
    assert report.failures[0].error_type == "LLMProviderContractError"
