"""Tests for independent human-label loading and offline metrics."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inbox_agent.evaluation import (
    ExpectedResultsJSONError,
    ExpectedResultsNotFoundError,
    ExpectedResultsValidationError,
    evaluate_analysis,
    load_expected_results,
)
from inbox_agent.models import Priority
from inbox_agent.pipeline import analyze_file

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rules.yaml"
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
EXPECTED_PATH = ROOT / "data" / "eval" / "expected_results.json"
EVALUATED_AT = datetime(2026, 8, 7, 18, 0, tzinfo=UTC)


def test_load_expected_results_reads_all_labels() -> None:
    expected = load_expected_results(EXPECTED_PATH)

    assert expected.dataset_version == "1.0"
    assert len(expected.labels) == 20
    assert expected.labels[0].expected_priority is Priority.P3


def test_load_expected_results_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ExpectedResultsNotFoundError, match="does not exist"):
        load_expected_results(tmp_path / "missing.json")


def test_load_expected_results_reports_invalid_json(tmp_path: Path) -> None:
    expected_path = tmp_path / "expected.json"
    expected_path.write_text('{"labels": [}', encoding="utf-8")

    with pytest.raises(ExpectedResultsJSONError, match=r"line 1, column \d+"):
        load_expected_results(expected_path)


def test_load_expected_results_rejects_duplicate_ids(tmp_path: Path) -> None:
    payload = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    payload["labels"].append(payload["labels"][0].copy())
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ExpectedResultsValidationError, match="InboxPilot schema"):
        load_expected_results(expected_path)


def test_perfect_regression_dataset_passes_all_metrics() -> None:
    analysis = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)
    expected = load_expected_results(EXPECTED_PATH)

    report = evaluate_analysis(analysis, expected)

    assert report.passed is True
    assert report.total_labels == 20
    assert report.evaluated_predictions == 20
    assert report.priority_accuracy == 1.0
    assert report.category_accuracy == 1.0
    assert report.review_accuracy == 1.0
    assert report.p1_precision == 1.0
    assert report.p1_recall == 1.0
    assert report.mismatches == ()


def test_changed_prediction_produces_detailed_mismatches() -> None:
    analysis = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)
    expected = load_expected_results(EXPECTED_PATH)
    first = analysis.results[0].model_copy(
        update={
            "priority": Priority.P5,
            "category": "general_notice",
            "requires_review": not analysis.results[0].requires_review,
        }
    )
    changed_analysis = analysis.model_copy(update={"results": (first, *analysis.results[1:])})

    report = evaluate_analysis(changed_analysis, expected)
    fields = {mismatch.field for mismatch in report.mismatches}

    assert report.passed is False
    assert report.priority_accuracy == 19 / 20
    assert report.category_accuracy == 19 / 20
    assert report.review_accuracy == 19 / 20
    assert fields == {"priority", "category", "requires_review"}


def test_missing_prediction_counts_as_incorrect() -> None:
    analysis = analyze_file(DATASET_PATH, POLICY_PATH, evaluated_at=EVALUATED_AT)
    expected = load_expected_results(EXPECTED_PATH)
    changed_analysis = analysis.model_copy(update={"results": analysis.results[1:]})

    report = evaluate_analysis(changed_analysis, expected)

    assert report.passed is False
    assert report.evaluated_predictions == 19
    assert report.priority_accuracy == 19 / 20
    assert any(mismatch.field == "missing_prediction" for mismatch in report.mismatches)
