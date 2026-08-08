"""HTTP-mocked tests for the real OpenAI-compatible provider adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import OpenAI
from pydantic import ValidationError

from inbox_agent.llm import (
    LLMProviderContractError,
    LLMProviderCredentialError,
    LLMProviderResponseError,
    LLMProviderSettingsNotFoundError,
    LLMProviderSettingsValidationError,
    LLMProviderSettingsYAMLError,
    LLMProviderUnavailableError,
    OpenAICompatibleProvider,
    OpenAICompatibleService,
    OpenAICompatibleSettings,
    load_openai_compatible_settings,
)
from inbox_agent.models import MailSource, NormalizedMessage


def make_message() -> NormalizedMessage:
    return NormalizedMessage(
        source=MailSource.MOCK,
        source_id="provider-sample-001",
        subject="课程项目补交提醒",
        from_name="示例教师",
        from_address="teacher@example.edu",
        from_domain="example.edu",
        to_addresses=("student@example.edu",),
        received_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
        body_text="请在 2026 年 8 月 8 日 20:00 前补交课程项目。",
    )


def analysis_payload() -> dict[str, Any]:
    return {
        "priority": "P1",
        "category": "academic_deadline",
        "summary": "课程项目需要在截止时间前补交。",
        "action_items": [
            {
                "description": "补交课程项目",
                "confidence": 0.96,
                "evidence": "请补交课程项目",
                "deadline": {
                    "value": "2026-08-08T20:00:00+00:00",
                    "kind": "explicit",
                    "confidence": 0.98,
                    "evidence": "2026 年 8 月 8 日 20:00 前",
                },
            }
        ],
        "deadline": {
            "value": "2026-08-08T20:00:00+00:00",
            "kind": "explicit",
            "confidence": 0.98,
            "evidence": "2026 年 8 月 8 日 20:00 前",
        },
        "confidence": 0.95,
        "rationale": "包含明确的课程任务和截止时间。",
        "requires_review": False,
    }


def settings(provider: OpenAICompatibleService) -> OpenAICompatibleSettings:
    return OpenAICompatibleSettings(
        provider=provider,
        model="test-model-v1",
        base_url="https://mock.example/v1",
        api_key_env="TEST_API_KEY",
        max_retries=0,
    )


def completion_payload(
    content: str,
    *,
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-provider-test",
        "object": "chat.completion",
        "created": 1_786_070_400,
        "model": "test-model-v1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": None},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage
        or {
            "prompt_tokens": 500,
            "completion_tokens": 120,
            "total_tokens": 620,
            "prompt_tokens_details": {"cached_tokens": 50},
        },
    }


def mock_provider(
    provider_type: OpenAICompatibleService,
    handler: httpx.MockTransport,
) -> OpenAICompatibleProvider:
    client = OpenAI(
        api_key="test-key",
        base_url="https://mock.example/v1",
        max_retries=0,
        http_client=httpx.Client(transport=handler),
    )
    times = iter((10.0, 10.125))
    return OpenAICompatibleProvider(
        settings(provider_type),
        client,
        clock=lambda: datetime(2026, 8, 8, 0, 0, tzinfo=UTC),
        timer=lambda: next(times),
    )


def test_loads_strict_provider_settings(tmp_path: Path) -> None:
    path = tmp_path / "provider.yaml"
    path.write_text(
        "\n".join(
            (
                "provider: deepseek",
                "model: deepseek-v4-flash",
                "base_url: https://api.deepseek.com/",
                "api_key_env: DEEPSEEK_API_KEY",
            )
        ),
        encoding="utf-8",
    )

    loaded = load_openai_compatible_settings(path)

    assert loaded.provider is OpenAICompatibleService.DEEPSEEK
    assert loaded.base_url == "https://api.deepseek.com"
    assert loaded.max_completion_tokens == 2_000


def test_settings_report_missing_invalid_yaml_and_schema(tmp_path: Path) -> None:
    with pytest.raises(LLMProviderSettingsNotFoundError):
        load_openai_compatible_settings(tmp_path / "missing.yaml")

    invalid_yaml = tmp_path / "invalid.yaml"
    invalid_yaml.write_text("provider: [", encoding="utf-8")
    with pytest.raises(LLMProviderSettingsYAMLError):
        load_openai_compatible_settings(invalid_yaml)

    invalid_schema = tmp_path / "invalid-schema.yaml"
    invalid_schema.write_text(
        "provider: openai\nmodel: test\nbase_url: http://unsafe.example\napi_key_env: key",
        encoding="utf-8",
    )
    with pytest.raises(LLMProviderSettingsValidationError) as captured:
        load_openai_compatible_settings(invalid_schema)
    assert captured.value.validation_error.error_count() == 2


def test_from_yaml_reads_key_from_named_environment_variable(tmp_path: Path) -> None:
    path = tmp_path / "provider.yaml"
    path.write_text(
        "provider: openai\n"
        "model: test-model-v1\n"
        "base_url: https://mock.example/v1\n"
        "api_key_env: TEST_API_KEY\n",
        encoding="utf-8",
    )

    provider = OpenAICompatibleProvider.from_yaml(path, environment={"TEST_API_KEY": "secret"})

    assert provider.provider_name == "openai"
    assert provider.model_name == "test-model-v1"

    with pytest.raises(LLMProviderCredentialError, match="TEST_API_KEY"):
        OpenAICompatibleProvider.from_yaml(path, environment={})


def test_openai_uses_native_structured_outputs_and_returns_audit_metadata() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=completion_payload(json.dumps(analysis_payload(), ensure_ascii=False)),
        )

    provider = mock_provider(OpenAICompatibleService.OPENAI, httpx.MockTransport(handler))
    result = provider.analyze(make_message())

    assert result.analysis.priority.value == "P1"
    assert result.provider == "openai"
    assert result.duration_ms == 125
    assert result.request_id == "chatcmpl-provider-test"
    assert result.usage is not None
    assert result.usage.input_tokens == 500
    assert result.usage.output_tokens == 120
    assert result.usage.cached_input_tokens == 50
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[0]["response_format"]["json_schema"]["strict"] is True
    assert requests[0]["store"] is False
    assert len(requests[0]["safety_identifier"]) == 64


def test_deepseek_uses_json_mode_and_validates_locally() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        payload = completion_payload(
            json.dumps(analysis_payload(), ensure_ascii=False),
            usage={
                "prompt_tokens": 400,
                "completion_tokens": 100,
                "total_tokens": 500,
                "prompt_cache_hit_tokens": 80,
            },
        )
        return httpx.Response(200, json=payload)

    provider = mock_provider(OpenAICompatibleService.DEEPSEEK, httpx.MockTransport(handler))
    result = provider.analyze(make_message())

    assert result.analysis.category.value == "academic_deadline"
    assert result.provider == "deepseek"
    assert result.usage is not None
    assert result.usage.cached_input_tokens == 80
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "store" not in requests[0]
    assert "safety_identifier" not in requests[0]
    assert "JSON" in requests[0]["messages"][0]["content"]
    assert '"$defs"' in requests[0]["messages"][0]["content"]
    assert '"action_items"' in requests[0]["messages"][0]["content"]
    assert '"requires_review"' in requests[0]["messages"][0]["content"]


def test_deepseek_rejects_invalid_and_empty_json() -> None:
    invalid = mock_provider(
        OpenAICompatibleService.DEEPSEEK,
        httpx.MockTransport(
            lambda _: httpx.Response(200, json=completion_payload('{"priority":"P1"}'))
        ),
    )
    with pytest.raises(LLMProviderContractError) as captured:
        invalid.analyze(make_message())
    assert isinstance(captured.value.validation_error, ValidationError)
    assert "category: Field required" in str(captured.value)

    empty = mock_provider(
        OpenAICompatibleService.DEEPSEEK,
        httpx.MockTransport(lambda _: httpx.Response(200, json=completion_payload(""))),
    )
    with pytest.raises(LLMProviderResponseError, match="empty JSON"):
        empty.analyze(make_message())


def test_provider_reports_truncation_and_http_failure() -> None:
    truncated = mock_provider(
        OpenAICompatibleService.DEEPSEEK,
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json=completion_payload(
                    json.dumps(analysis_payload()),
                    finish_reason="length",
                ),
            )
        ),
    )
    with pytest.raises(LLMProviderResponseError, match="finish_reason=length"):
        truncated.analyze(make_message())

    unavailable = mock_provider(
        OpenAICompatibleService.DEEPSEEK,
        httpx.MockTransport(lambda _: httpx.Response(500, json={"error": {"message": "down"}})),
    )
    with pytest.raises(LLMProviderUnavailableError, match="down"):
        unavailable.analyze(make_message())
