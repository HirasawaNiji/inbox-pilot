"""Tests for versioned offline structured-response fixtures."""

import json
from pathlib import Path

import pytest

from inbox_agent.llm import (
    FakeLLMResponsesJSONError,
    FakeLLMResponsesNotFoundError,
    FakeLLMResponsesValidationError,
    load_fake_llm_responses,
)
from inbox_agent.models import Priority

ROOT = Path(__file__).resolve().parents[1]
RESPONSES_PATH = ROOT / "data" / "eval" / "fake_llm_responses.json"


def test_load_fake_llm_responses_reads_strict_versioned_fixture() -> None:
    responses = load_fake_llm_responses(RESPONSES_PATH)

    assert responses.dataset_version == "llm-eval-v1"
    assert responses.prompt_version == "triage-v1"
    assert len(responses.responses) == 8
    assert responses.responses[0].analysis.priority is Priority.P1
    assert set(responses.as_mapping()) == {response.source_id for response in responses.responses}


def test_load_fake_llm_responses_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FakeLLMResponsesNotFoundError, match="does not exist"):
        load_fake_llm_responses(tmp_path / "missing.json")


def test_load_fake_llm_responses_reports_invalid_json(tmp_path: Path) -> None:
    response_path = tmp_path / "responses.json"
    response_path.write_text('{"responses": [}', encoding="utf-8")

    with pytest.raises(FakeLLMResponsesJSONError, match=r"line 1, column \d+"):
        load_fake_llm_responses(response_path)


def test_load_fake_llm_responses_rejects_duplicate_ids(tmp_path: Path) -> None:
    payload = json.loads(RESPONSES_PATH.read_text(encoding="utf-8"))
    payload["responses"].append(payload["responses"][0].copy())
    response_path = tmp_path / "responses.json"
    response_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(FakeLLMResponsesValidationError, match="InboxPilot schema"):
        load_fake_llm_responses(response_path)


def test_load_fake_llm_responses_rejects_extra_analysis_fields(tmp_path: Path) -> None:
    payload = json.loads(RESPONSES_PATH.read_text(encoding="utf-8"))
    payload["responses"][0]["analysis"]["invented_field"] = "not allowed"
    response_path = tmp_path / "responses.json"
    response_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(FakeLLMResponsesValidationError, match="InboxPilot schema"):
        load_fake_llm_responses(response_path)
