"""Evaluate structured LLM sidecar analyses against human-authored labels."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Self

from pydantic import Field, ValidationError, field_validator, model_validator

from inbox_agent.models import (
    DeadlineKind,
    FrozenModel,
    MessageCategory,
    Priority,
)
from inbox_agent.pipeline import AnalysisReport


class ExpectedLLMDeadline(FrozenModel):
    """Expected absolute deadline and whether it was explicit or inferred."""

    value: datetime
    kind: DeadlineKind

    @field_validator("value")
    @classmethod
    def validate_aware_datetime(cls, value: datetime) -> datetime:
        """Require enough timezone context for exact deadline comparison."""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected LLM deadline must include timezone information")
        return value


class ExpectedLLMLabel(FrozenModel):
    """Human-authored semantic expectations for one anonymous message."""

    source_id: str = Field(min_length=1, max_length=512)
    expected_priority: Priority
    expected_category: MessageCategory
    summary_facts: tuple[str, ...] = Field(min_length=1)
    expected_action_phrases: tuple[str, ...] = ()
    expected_deadline: ExpectedLLMDeadline | None
    requires_review: bool
    explanation: str = Field(min_length=1, max_length=1_000)

    @field_validator("summary_facts", "expected_action_phrases")
    @classmethod
    def validate_unique_non_empty_phrases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Keep phrase-based evaluation deterministic and unambiguous."""

        normalized = [value.casefold().strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("evaluation phrases must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("evaluation phrases must be unique")
        return values


class ExpectedLLMResults(FrozenModel):
    """Versioned human labels for structured LLM sidecar evaluation."""

    dataset_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=100,
    )
    source_dataset: str = Field(min_length=1, max_length=1_000)
    prompt_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=100,
    )
    labels: tuple[ExpectedLLMLabel, ...]

    @model_validator(mode="after")
    def validate_unique_source_ids(self) -> Self:
        """Require exactly one human label per source message ID."""

        source_ids = [label.source_id for label in self.labels]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("expected LLM results contain duplicate source IDs")
        return self


class ExpectedLLMResultsLoadError(Exception):
    """Base class for structured LLM label loading failures."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class ExpectedLLMResultsNotFoundError(ExpectedLLMResultsLoadError):
    """Raised when a structured LLM label file is missing."""


class ExpectedLLMResultsReadError(ExpectedLLMResultsLoadError):
    """Raised when a structured LLM label file cannot be read."""


class ExpectedLLMResultsJSONError(ExpectedLLMResultsLoadError):
    """Raised when a structured LLM label file contains invalid JSON."""


class ExpectedLLMResultsValidationError(ExpectedLLMResultsLoadError):
    """Raised when structured LLM labels violate their strict schema."""

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(path, "Expected LLM results do not match the InboxPilot schema")


class LLMEvaluationMismatch(FrozenModel):
    """One semantic difference between a sidecar analysis and human label."""

    source_id: str
    field: str
    expected: str | bool | None
    actual: str | bool | None


class LLMEvaluationReport(FrozenModel):
    """Aggregate structured-output metrics and detailed mismatches."""

    dataset_version: str
    prompt_version: str
    total_labels: int = Field(ge=0)
    evaluated_predictions: int = Field(ge=0)
    llm_failure_count: int = Field(ge=0)

    priority_correct: int = Field(ge=0)
    category_correct: int = Field(ge=0)
    summary_correct: int = Field(ge=0)
    action_items_correct: int = Field(ge=0)
    deadline_correct: int = Field(ge=0)
    review_correct: int = Field(ge=0)

    priority_accuracy: float = Field(ge=0.0, le=1.0)
    category_accuracy: float = Field(ge=0.0, le=1.0)
    summary_accuracy: float = Field(ge=0.0, le=1.0)
    action_items_accuracy: float = Field(ge=0.0, le=1.0)
    deadline_accuracy: float = Field(ge=0.0, le=1.0)
    review_accuracy: float = Field(ge=0.0, le=1.0)

    passed: bool
    mismatches: tuple[LLMEvaluationMismatch, ...] = ()


def load_expected_llm_results(path: str | Path) -> ExpectedLLMResults:
    """Read and validate a UTF-8 structured LLM label file."""

    expected_path = Path(path)
    try:
        raw_content = expected_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ExpectedLLMResultsNotFoundError(
            expected_path,
            "Expected LLM result file does not exist",
        ) from error
    except OSError as error:
        raise ExpectedLLMResultsReadError(
            expected_path,
            "Unable to read expected LLM result file",
        ) from error

    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as error:
        location = f"Invalid JSON at line {error.lineno}, column {error.colno}"
        raise ExpectedLLMResultsJSONError(expected_path, location) from error

    try:
        return ExpectedLLMResults.model_validate(payload)
    except ValidationError as error:
        raise ExpectedLLMResultsValidationError(expected_path, error) from error


def _ratio(numerator: int, denominator: int) -> float:
    """Calculate a bounded metric, returning zero for an empty denominator."""

    return numerator / denominator if denominator else 0.0


def _format_deadline(deadline: ExpectedLLMDeadline | None) -> str | None:
    """Return a compact, stable deadline representation for mismatch output."""

    if deadline is None:
        return None
    return f"{deadline.value.isoformat()} ({deadline.kind.value})"


def _summary_matches(summary: str, required_facts: tuple[str, ...]) -> bool:
    """Accept a summary only when every human-selected fact is present."""

    normalized_summary = summary.casefold()
    return all(fact.casefold() in normalized_summary for fact in required_facts)


def _action_items_match(
    descriptions: tuple[str, ...],
    expected_phrases: tuple[str, ...],
) -> bool:
    """Compare action count and human-selected phrases without exact wording."""

    if len(descriptions) != len(expected_phrases):
        return False
    normalized_descriptions = tuple(description.casefold() for description in descriptions)
    return all(
        any(phrase.casefold() in description for description in normalized_descriptions)
        for phrase in expected_phrases
    )


def evaluate_llm_analysis(
    analysis: AnalysisReport,
    expected: ExpectedLLMResults,
) -> LLMEvaluationReport:
    """Compare sidecar structured analyses with independent human labels."""

    predictions = {result.message_id: result for result in analysis.llm_analyses}
    labels = {label.source_id: label for label in expected.labels}
    mismatches: list[LLMEvaluationMismatch] = []
    priority_correct = 0
    category_correct = 0
    summary_correct = 0
    action_items_correct = 0
    deadline_correct = 0
    review_correct = 0

    for source_id, label in labels.items():
        prediction_result = predictions.get(source_id)
        if prediction_result is None:
            mismatches.append(
                LLMEvaluationMismatch(
                    source_id=source_id,
                    field="missing_prediction",
                    expected="prediction",
                    actual=None,
                )
            )
            continue
        prediction = prediction_result.analysis

        if prediction_result.prompt_version != expected.prompt_version:
            mismatches.append(
                LLMEvaluationMismatch(
                    source_id=source_id,
                    field="prompt_version",
                    expected=expected.prompt_version,
                    actual=prediction_result.prompt_version,
                )
            )

        if prediction.priority is label.expected_priority:
            priority_correct += 1
        else:
            mismatches.append(
                LLMEvaluationMismatch(
                    source_id=source_id,
                    field="priority",
                    expected=label.expected_priority.value,
                    actual=prediction.priority.value,
                )
            )

        if prediction.category is label.expected_category:
            category_correct += 1
        else:
            mismatches.append(
                LLMEvaluationMismatch(
                    source_id=source_id,
                    field="category",
                    expected=label.expected_category.value,
                    actual=prediction.category.value,
                )
            )

        if _summary_matches(prediction.summary, label.summary_facts):
            summary_correct += 1
        else:
            mismatches.append(
                LLMEvaluationMismatch(
                    source_id=source_id,
                    field="summary",
                    expected="; ".join(label.summary_facts),
                    actual=prediction.summary,
                )
            )

        action_descriptions = tuple(item.description for item in prediction.action_items)
        if _action_items_match(action_descriptions, label.expected_action_phrases):
            action_items_correct += 1
        else:
            mismatches.append(
                LLMEvaluationMismatch(
                    source_id=source_id,
                    field="action_items",
                    expected="; ".join(label.expected_action_phrases),
                    actual="; ".join(action_descriptions),
                )
            )

        predicted_deadline = (
            None
            if prediction.deadline is None
            else ExpectedLLMDeadline(
                value=prediction.deadline.value,
                kind=prediction.deadline.kind,
            )
        )
        if predicted_deadline == label.expected_deadline:
            deadline_correct += 1
        else:
            mismatches.append(
                LLMEvaluationMismatch(
                    source_id=source_id,
                    field="deadline",
                    expected=_format_deadline(label.expected_deadline),
                    actual=_format_deadline(predicted_deadline),
                )
            )

        if prediction.requires_review is label.requires_review:
            review_correct += 1
        else:
            mismatches.append(
                LLMEvaluationMismatch(
                    source_id=source_id,
                    field="requires_review",
                    expected=label.requires_review,
                    actual=prediction.requires_review,
                )
            )

    for source_id in predictions.keys() - labels.keys():
        mismatches.append(
            LLMEvaluationMismatch(
                source_id=source_id,
                field="unexpected_prediction",
                expected=None,
                actual="prediction",
            )
        )

    total_labels = len(labels)
    evaluated_predictions = len(predictions.keys() & labels.keys())
    mismatches.sort(key=lambda item: (item.source_id, item.field))

    return LLMEvaluationReport(
        dataset_version=expected.dataset_version,
        prompt_version=expected.prompt_version,
        total_labels=total_labels,
        evaluated_predictions=evaluated_predictions,
        llm_failure_count=analysis.llm_failure_count,
        priority_correct=priority_correct,
        category_correct=category_correct,
        summary_correct=summary_correct,
        action_items_correct=action_items_correct,
        deadline_correct=deadline_correct,
        review_correct=review_correct,
        priority_accuracy=_ratio(priority_correct, total_labels),
        category_accuracy=_ratio(category_correct, total_labels),
        summary_accuracy=_ratio(summary_correct, total_labels),
        action_items_accuracy=_ratio(action_items_correct, total_labels),
        deadline_accuracy=_ratio(deadline_correct, total_labels),
        review_accuracy=_ratio(review_correct, total_labels),
        passed=not mismatches and analysis.llm_failure_count == 0,
        mismatches=tuple(mismatches),
    )
