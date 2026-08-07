"""End-to-end tests for semantic evaluation of LLM sidecar analyses."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inbox_agent.llm import (
    FakeLLMProvider,
    LLMFusionEngine,
    LLMRouter,
    load_fake_llm_responses,
)
from inbox_agent.llm.evaluation import (
    ExpectedLLMResultsJSONError,
    ExpectedLLMResultsNotFoundError,
    ExpectedLLMResultsValidationError,
    evaluate_llm_analysis,
    load_expected_llm_results,
)
from inbox_agent.loader import load_dataset
from inbox_agent.pipeline import AnalysisReport, OfflinePipeline

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rules.yaml"
DATASET_PATH = ROOT / "data" / "samples" / "llm_evaluation_emails.json"
EXPECTED_PATH = ROOT / "data" / "eval" / "expected_llm_results.json"
RESPONSES_PATH = ROOT / "data" / "eval" / "fake_llm_responses.json"
EVALUATED_AT = datetime(2026, 8, 8, 18, 0, tzinfo=UTC)


def build_analysis_report() -> AnalysisReport:
    """Run the independent semantic dataset through rules and fake sidecar."""

    responses = load_fake_llm_responses(RESPONSES_PATH)
    provider = FakeLLMProvider(
        responses.as_mapping(),
        prompt_version=responses.prompt_version,
        clock=lambda: EVALUATED_AT,
    )
    pipeline = OfflinePipeline.from_yaml(
        POLICY_PATH,
        llm_provider=provider,
        llm_router=LLMRouter.analyze_all(),
        llm_fusion=LLMFusionEngine.sidecar_only(),
    )
    return pipeline.analyze_file(DATASET_PATH, evaluated_at=EVALUATED_AT)


def test_llm_evaluation_dataset_and_labels_have_matching_ids() -> None:
    dataset = load_dataset(DATASET_PATH)
    expected = load_expected_llm_results(EXPECTED_PATH)
    responses = load_fake_llm_responses(RESPONSES_PATH)

    message_ids = {message.source_id for message in dataset.messages}
    label_ids = {label.source_id for label in expected.labels}
    response_ids = set(responses.as_mapping())

    assert len(message_ids) == 8
    assert message_ids == label_ids == response_ids
    assert expected.source_dataset == responses.source_dataset
    assert expected.prompt_version == responses.prompt_version


def test_load_expected_llm_results_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ExpectedLLMResultsNotFoundError, match="does not exist"):
        load_expected_llm_results(tmp_path / "missing.json")


def test_load_expected_llm_results_reports_invalid_json(tmp_path: Path) -> None:
    expected_path = tmp_path / "expected.json"
    expected_path.write_text('{"labels": [}', encoding="utf-8")

    with pytest.raises(ExpectedLLMResultsJSONError, match=r"line 1, column \d+"):
        load_expected_llm_results(expected_path)


def test_load_expected_llm_results_rejects_duplicate_ids(tmp_path: Path) -> None:
    payload = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    payload["labels"].append(payload["labels"][0].copy())
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ExpectedLLMResultsValidationError, match="InboxPilot schema"):
        load_expected_llm_results(expected_path)


def test_perfect_fake_sidecar_passes_all_semantic_metrics() -> None:
    analysis = build_analysis_report()
    expected = load_expected_llm_results(EXPECTED_PATH)

    report = evaluate_llm_analysis(analysis, expected)

    assert analysis.processed_count == 8
    assert analysis.failure_count == 0
    assert analysis.llm_analysis_count == 8
    assert report.passed is True
    assert report.total_labels == 8
    assert report.evaluated_predictions == 8
    assert report.llm_failure_count == 0
    assert report.priority_accuracy == 1.0
    assert report.category_accuracy == 1.0
    assert report.summary_accuracy == 1.0
    assert report.action_items_accuracy == 1.0
    assert report.deadline_accuracy == 1.0
    assert report.review_accuracy == 1.0
    assert report.mismatches == ()


def test_changed_semantic_fields_produce_detailed_mismatches() -> None:
    analysis = build_analysis_report()
    expected = load_expected_llm_results(EXPECTED_PATH)
    first = analysis.llm_analyses[0]
    changed_result = first.model_copy(
        update={
            "analysis": first.analysis.model_copy(
                update={
                    "summary": "不包含人工标注事实",
                    "action_items": (),
                    "deadline": None,
                }
            )
        }
    )
    changed_analysis = analysis.model_copy(
        update={"llm_analyses": (changed_result, *analysis.llm_analyses[1:])}
    )

    report = evaluate_llm_analysis(changed_analysis, expected)
    fields = {mismatch.field for mismatch in report.mismatches}

    assert report.passed is False
    assert report.summary_accuracy == 7 / 8
    assert report.action_items_accuracy == 7 / 8
    assert report.deadline_accuracy == 7 / 8
    assert fields == {"summary", "action_items", "deadline"}


def test_wrong_prompt_version_cannot_pass_evaluation() -> None:
    analysis = build_analysis_report()
    expected = load_expected_llm_results(EXPECTED_PATH)
    first = analysis.llm_analyses[0].model_copy(update={"prompt_version": "triage-v0"})
    changed_analysis = analysis.model_copy(
        update={"llm_analyses": (first, *analysis.llm_analyses[1:])}
    )

    report = evaluate_llm_analysis(changed_analysis, expected)

    assert report.passed is False
    assert any(mismatch.field == "prompt_version" for mismatch in report.mismatches)


def test_missing_fake_response_isolated_from_rule_results() -> None:
    responses = load_fake_llm_responses(RESPONSES_PATH)
    mapping = responses.as_mapping()
    missing_id = next(iter(mapping))
    del mapping[missing_id]
    provider = FakeLLMProvider(mapping, clock=lambda: EVALUATED_AT)
    pipeline = OfflinePipeline.from_yaml(
        POLICY_PATH,
        llm_provider=provider,
        llm_router=LLMRouter.analyze_all(),
        llm_fusion=LLMFusionEngine.sidecar_only(),
    )

    analysis = pipeline.analyze_file(DATASET_PATH, evaluated_at=EVALUATED_AT)
    report = evaluate_llm_analysis(analysis, load_expected_llm_results(EXPECTED_PATH))

    assert analysis.processed_count == 8
    assert analysis.failure_count == 0
    assert analysis.llm_analysis_count == 7
    assert analysis.llm_failure_count == 1
    assert report.passed is False
    assert report.evaluated_predictions == 7
    assert report.llm_failure_count == 1
    assert any(
        mismatch.source_id == missing_id and mismatch.field == "missing_prediction"
        for mismatch in report.mismatches
    )
