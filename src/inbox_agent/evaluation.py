"""Offline evaluation against human-authored expected results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from pydantic import Field, ValidationError, model_validator

from inbox_agent.models import FrozenModel, Priority
from inbox_agent.pipeline import AnalysisReport


class ExpectedLabel(FrozenModel):
    """Human-authored expected outcome for one anonymous sample message."""

    source_id: str = Field(min_length=1, max_length=512)
    expected_priority: Priority
    expected_category: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    expected_signals: tuple[str, ...] = ()
    requires_review: bool = False
    explanation: str = Field(min_length=1, max_length=1_000)
    llm_expected_priority: Priority | None = None
    llm_acceptable_priorities: tuple[Priority, ...] = ()
    llm_expected_category: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=100,
    )

    @model_validator(mode="after")
    def validate_llm_priority_policy(self) -> Self:
        """Keep model-specific alternatives explicit, unique, and non-canonical."""

        if len(self.llm_acceptable_priorities) != len(set(self.llm_acceptable_priorities)):
            raise ValueError("LLM acceptable priorities contain duplicates")
        if self.validation_priority in self.llm_acceptable_priorities:
            raise ValueError("LLM expected priority must not also be an acceptable alternative")
        return self

    @property
    def validation_priority(self) -> Priority:
        """Return the canonical priority used for raw LLM validation."""

        return self.llm_expected_priority or self.expected_priority

    @property
    def validation_category(self) -> str:
        """Return the canonical category used for raw LLM validation."""

        return self.llm_expected_category or self.expected_category


class ExpectedResults(FrozenModel):
    """Versioned collection of human labels for one source dataset."""

    dataset_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=100)
    source_dataset: str = Field(min_length=1, max_length=1_000)
    labels: tuple[ExpectedLabel, ...]

    @model_validator(mode="after")
    def validate_unique_source_ids(self) -> Self:
        """Require exactly one human label per source message ID."""

        source_ids = [label.source_id for label in self.labels]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("expected results contain duplicate source IDs")
        return self


class ExpectedResultsLoadError(Exception):
    """Base class for expected-result loading failures."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class ExpectedResultsNotFoundError(ExpectedResultsLoadError):
    """Raised when the expected-result file is missing."""


class ExpectedResultsReadError(ExpectedResultsLoadError):
    """Raised when the expected-result file cannot be read."""


class ExpectedResultsJSONError(ExpectedResultsLoadError):
    """Raised when the expected-result file contains invalid JSON."""


class ExpectedResultsValidationError(ExpectedResultsLoadError):
    """Raised when JSON does not match the expected-result schema."""

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(path, "Expected results do not match the InboxPilot schema")


class EvaluationMismatch(FrozenModel):
    """One difference between a prediction and its human label."""

    source_id: str
    field: str
    expected: str | bool | None
    actual: str | bool | None


class EvaluationReport(FrozenModel):
    """Aggregate metrics and mismatch details for one evaluation run."""

    dataset_version: str
    policy_version: str
    total_labels: int = Field(ge=0)
    evaluated_predictions: int = Field(ge=0)
    analysis_failure_count: int = Field(ge=0)

    priority_correct: int = Field(ge=0)
    category_correct: int = Field(ge=0)
    review_correct: int = Field(ge=0)

    expected_p1: int = Field(ge=0)
    predicted_p1: int = Field(ge=0)
    true_positive_p1: int = Field(ge=0)

    priority_accuracy: float = Field(ge=0.0, le=1.0)
    category_accuracy: float = Field(ge=0.0, le=1.0)
    review_accuracy: float = Field(ge=0.0, le=1.0)
    p1_precision: float = Field(ge=0.0, le=1.0)
    p1_recall: float = Field(ge=0.0, le=1.0)

    passed: bool
    mismatches: tuple[EvaluationMismatch, ...] = ()


def load_expected_results(path: str | Path) -> ExpectedResults:
    """Read and validate a UTF-8 expected-result JSON file."""

    expected_path = Path(path)
    try:
        raw_content = expected_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ExpectedResultsNotFoundError(
            expected_path,
            "Expected-result file does not exist",
        ) from error
    except OSError as error:
        raise ExpectedResultsReadError(
            expected_path,
            "Unable to read expected-result file",
        ) from error

    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as error:
        location = f"Invalid JSON at line {error.lineno}, column {error.colno}"
        raise ExpectedResultsJSONError(expected_path, location) from error

    try:
        return ExpectedResults.model_validate(payload)
    except ValidationError as error:
        raise ExpectedResultsValidationError(expected_path, error) from error


def _ratio(numerator: int, denominator: int) -> float:
    """Calculate a bounded metric, returning zero for an empty denominator."""

    return numerator / denominator if denominator else 0.0


def evaluate_analysis(
    analysis: AnalysisReport,
    expected: ExpectedResults,
) -> EvaluationReport:
    """Compare deterministic predictions with independent human labels."""

    predictions = {result.message_id: result for result in analysis.results}
    labels = {label.source_id: label for label in expected.labels}
    mismatches: list[EvaluationMismatch] = []
    priority_correct = 0
    category_correct = 0
    review_correct = 0
    true_positive_p1 = 0

    for source_id, label in labels.items():
        prediction = predictions.get(source_id)
        if prediction is None:
            mismatches.append(
                EvaluationMismatch(
                    source_id=source_id,
                    field="missing_prediction",
                    expected="prediction",
                    actual=None,
                )
            )
            continue

        if prediction.priority is label.expected_priority:
            priority_correct += 1
            if label.expected_priority is Priority.P1:
                true_positive_p1 += 1
        else:
            mismatches.append(
                EvaluationMismatch(
                    source_id=source_id,
                    field="priority",
                    expected=label.expected_priority.value,
                    actual=prediction.priority.value,
                )
            )

        if prediction.category == label.expected_category:
            category_correct += 1
        else:
            mismatches.append(
                EvaluationMismatch(
                    source_id=source_id,
                    field="category",
                    expected=label.expected_category,
                    actual=prediction.category,
                )
            )

        if prediction.requires_review is label.requires_review:
            review_correct += 1
        else:
            mismatches.append(
                EvaluationMismatch(
                    source_id=source_id,
                    field="requires_review",
                    expected=label.requires_review,
                    actual=prediction.requires_review,
                )
            )

    for source_id in predictions.keys() - labels.keys():
        mismatches.append(
            EvaluationMismatch(
                source_id=source_id,
                field="unexpected_prediction",
                expected=None,
                actual="prediction",
            )
        )

    total_labels = len(labels)
    evaluated_predictions = len(predictions.keys() & labels.keys())
    expected_p1 = sum(label.expected_priority is Priority.P1 for label in expected.labels)
    predicted_p1 = sum(result.priority is Priority.P1 for result in analysis.results)
    mismatches.sort(key=lambda item: (item.source_id, item.field))

    return EvaluationReport(
        dataset_version=expected.dataset_version,
        policy_version=analysis.policy_version,
        total_labels=total_labels,
        evaluated_predictions=evaluated_predictions,
        analysis_failure_count=analysis.failure_count,
        priority_correct=priority_correct,
        category_correct=category_correct,
        review_correct=review_correct,
        expected_p1=expected_p1,
        predicted_p1=predicted_p1,
        true_positive_p1=true_positive_p1,
        priority_accuracy=_ratio(priority_correct, total_labels),
        category_accuracy=_ratio(category_correct, total_labels),
        review_accuracy=_ratio(review_correct, total_labels),
        p1_precision=_ratio(true_positive_p1, predicted_p1),
        p1_recall=_ratio(true_positive_p1, expected_p1),
        passed=not mismatches and analysis.failure_count == 0,
        mismatches=tuple(mismatches),
    )
