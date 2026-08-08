"""Classification-focused validation for real LLM providers on public samples."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field

from inbox_agent.models import FrozenModel

if TYPE_CHECKING:
    from inbox_agent.evaluation import ExpectedResults
    from inbox_agent.pipeline import AnalysisReport

MIN_PRIORITY_ACCURACY = 0.80
MIN_CATEGORY_ACCURACY = 0.80
MIN_REVIEW_ACCURACY = 0.80
MIN_EXACT_MATCH_ACCURACY = 0.70


class LLMValidationMismatch(FrozenModel):
    """One missing or incorrect classification field from a real provider."""

    message_id: str = Field(min_length=1, max_length=512)
    field: Literal["missing_analysis", "priority", "category", "requires_review"]
    expected: str
    actual: str | None = None


class LLMValidationFailure(FrozenModel):
    """One bounded provider, routing, or fusion failure from validation."""

    message_id: str = Field(min_length=1, max_length=512)
    stage: str = Field(min_length=1, max_length=100)
    error_type: str = Field(min_length=1, max_length=200)
    error_message: str = Field(min_length=1, max_length=500)


class LLMValidationTolerance(FrozenModel):
    """One adjacent priority accepted by an explicit human-label policy."""

    message_id: str = Field(min_length=1, max_length=512)
    expected: str
    actual: str


class LLMValidationReport(FrozenModel):
    """Metrics, usage, and mismatches for a full-provider validation run."""

    dataset_version: str
    provider: str | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    total_labels: int = Field(ge=1)
    analyzed_count: int = Field(ge=0)
    provider_failure_count: int = Field(ge=0)
    priority_accuracy: float = Field(ge=0.0, le=1.0)
    tolerated_priority_accuracy: float = Field(ge=0.0, le=1.0)
    category_accuracy: float = Field(ge=0.0, le=1.0)
    review_accuracy: float = Field(ge=0.0, le=1.0)
    exact_match_accuracy: float = Field(ge=0.0, le=1.0)
    tolerated_exact_match_accuracy: float = Field(ge=0.0, le=1.0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    usage_reported_count: int = Field(ge=0)
    total_duration_ms: int = Field(ge=0)
    passed: bool
    failures: tuple[LLMValidationFailure, ...] = ()
    tolerances: tuple[LLMValidationTolerance, ...] = ()
    mismatches: tuple[LLMValidationMismatch, ...] = ()


def _single_metadata(values: set[str]) -> str | None:
    if not values:
        return None
    if len(values) == 1:
        return next(iter(values))
    return "mixed"


def validate_llm_classifications(
    analysis: AnalysisReport,
    expected: ExpectedResults,
    *,
    provider_name: str | None = None,
    model_name: str | None = None,
    prompt_version: str | None = None,
) -> LLMValidationReport:
    """Compare raw sidecar LLM classifications with independent labels."""

    by_message_id = {result.message_id: result for result in analysis.llm_analyses}
    priority_correct = 0
    tolerated_priority_correct = 0
    category_correct = 0
    review_correct = 0
    exact_correct = 0
    tolerated_exact_correct = 0
    tolerances: list[LLMValidationTolerance] = []
    mismatches: list[LLMValidationMismatch] = []

    for label in expected.labels:
        result = by_message_id.get(label.source_id)
        if result is None:
            mismatches.append(
                LLMValidationMismatch(
                    message_id=label.source_id,
                    field="missing_analysis",
                    expected="structured LLM analysis",
                )
            )
            continue

        expected_priority = label.validation_priority
        expected_category = label.validation_category
        priority_matches = result.analysis.priority is expected_priority
        priority_is_tolerated = (
            priority_matches or result.analysis.priority in label.llm_acceptable_priorities
        )
        category_matches = result.analysis.category.value == expected_category
        review_matches = result.analysis.requires_review is label.requires_review

        priority_correct += int(priority_matches)
        tolerated_priority_correct += int(priority_is_tolerated)
        category_correct += int(category_matches)
        review_correct += int(review_matches)
        exact_correct += int(priority_matches and category_matches and review_matches)
        tolerated_exact_correct += int(
            priority_is_tolerated and category_matches and review_matches
        )

        if priority_is_tolerated and not priority_matches:
            tolerances.append(
                LLMValidationTolerance(
                    message_id=label.source_id,
                    expected=expected_priority.value,
                    actual=result.analysis.priority.value,
                )
            )
        elif not priority_is_tolerated:
            accepted = "/".join(
                priority.value for priority in (expected_priority, *label.llm_acceptable_priorities)
            )
            mismatches.append(
                LLMValidationMismatch(
                    message_id=label.source_id,
                    field="priority",
                    expected=accepted,
                    actual=result.analysis.priority.value,
                )
            )
        if not category_matches:
            mismatches.append(
                LLMValidationMismatch(
                    message_id=label.source_id,
                    field="category",
                    expected=expected_category,
                    actual=result.analysis.category.value,
                )
            )
        if not review_matches:
            mismatches.append(
                LLMValidationMismatch(
                    message_id=label.source_id,
                    field="requires_review",
                    expected=str(label.requires_review).lower(),
                    actual=str(result.analysis.requires_review).lower(),
                )
            )

    denominator = len(expected.labels)
    priority_accuracy = priority_correct / denominator
    tolerated_priority_accuracy = tolerated_priority_correct / denominator
    category_accuracy = category_correct / denominator
    review_accuracy = review_correct / denominator
    exact_match_accuracy = exact_correct / denominator
    tolerated_exact_match_accuracy = tolerated_exact_correct / denominator

    usages = [result.usage for result in analysis.llm_analyses if result.usage is not None]
    expected_message_ids = {label.source_id for label in expected.labels}
    complete_coverage = set(by_message_id) == expected_message_ids
    passed = bool(
        complete_coverage
        and not analysis.llm_failures
        and tolerated_priority_accuracy >= MIN_PRIORITY_ACCURACY
        and category_accuracy >= MIN_CATEGORY_ACCURACY
        and review_accuracy >= MIN_REVIEW_ACCURACY
        and tolerated_exact_match_accuracy >= MIN_EXACT_MATCH_ACCURACY
    )

    return LLMValidationReport(
        dataset_version=expected.dataset_version,
        provider=(
            _single_metadata({result.provider for result in analysis.llm_analyses}) or provider_name
        ),
        model_name=(
            _single_metadata({result.model_name for result in analysis.llm_analyses}) or model_name
        ),
        prompt_version=(
            _single_metadata({result.prompt_version for result in analysis.llm_analyses})
            or prompt_version
        ),
        total_labels=denominator,
        analyzed_count=len(by_message_id),
        provider_failure_count=len(analysis.llm_failures),
        priority_accuracy=priority_accuracy,
        tolerated_priority_accuracy=tolerated_priority_accuracy,
        category_accuracy=category_accuracy,
        review_accuracy=review_accuracy,
        exact_match_accuracy=exact_match_accuracy,
        tolerated_exact_match_accuracy=tolerated_exact_match_accuracy,
        input_tokens=sum(usage.input_tokens for usage in usages),
        output_tokens=sum(usage.output_tokens for usage in usages),
        cached_input_tokens=sum(usage.cached_input_tokens for usage in usages),
        usage_reported_count=len(usages),
        total_duration_ms=sum(result.duration_ms for result in analysis.llm_analyses),
        passed=passed,
        failures=tuple(
            LLMValidationFailure(
                message_id=failure.message_id,
                stage=failure.stage,
                error_type=failure.error_type,
                error_message=failure.error_message,
            )
            for failure in analysis.llm_failures
        ),
        tolerances=tuple(tolerances),
        mismatches=tuple(mismatches),
    )
