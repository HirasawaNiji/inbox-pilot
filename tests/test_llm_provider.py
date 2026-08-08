"""Tests for the provider-neutral LLM interface and offline fake."""

from datetime import UTC, datetime

import pytest

from inbox_agent.llm import (
    FakeLLMProvider,
    LLMProvider,
    LLMProviderContractError,
    LLMProviderUnavailableError,
    LLMResponseNotConfiguredError,
)
from inbox_agent.models import (
    ActionItem,
    DeadlineKind,
    ExtractedDeadline,
    LLMMessageAnalysis,
    LLMTokenUsage,
    MailSource,
    NormalizedMessage,
    Priority,
)


def make_message(source_id: str = "sample-001") -> NormalizedMessage:
    """Build one normalized message accepted by every provider."""

    return NormalizedMessage(
        source=MailSource.MOCK,
        source_id=source_id,
        subject="课程项目补交提醒",
        from_name="示例教师",
        from_address="teacher@example.edu",
        from_domain="example.edu",
        to_addresses=("student@example.edu",),
        received_at=datetime(2026, 8, 7, 10, 0, tzinfo=UTC),
        body_text="请在 2026 年 8 月 8 日 20:00 前补交课程项目。",
    )


def make_analysis() -> LLMMessageAnalysis:
    """Build one valid strict structured analysis."""

    deadline = ExtractedDeadline(
        value=datetime(2026, 8, 8, 20, 0, tzinfo=UTC),
        kind=DeadlineKind.EXPLICIT,
        confidence=0.98,
        evidence="2026 年 8 月 8 日 20:00 前",
    )
    return LLMMessageAnalysis(
        priority=Priority.P1,
        category="academic_deadline",
        summary="课程项目需要在截止时间前补交。",
        action_items=(
            ActionItem(
                description="补交课程项目",
                confidence=0.96,
                evidence="请补交课程项目",
                deadline=deadline,
            ),
        ),
        deadline=deadline,
        confidence=0.95,
        rationale="包含明确的课程任务和截止时间。",
        requires_review=False,
    )


def fixed_clock() -> datetime:
    """Return a deterministic timezone-aware invocation time."""

    return datetime(2026, 8, 7, 14, 0, tzinfo=UTC)


def test_fake_provider_implements_runtime_protocol() -> None:
    provider = FakeLLMProvider({"sample-001": make_analysis()})

    assert isinstance(provider, LLMProvider)


def test_fake_provider_returns_traceable_result_and_records_call() -> None:
    usage = LLMTokenUsage(input_tokens=900, output_tokens=150)
    provider = FakeLLMProvider(
        {"sample-001": make_analysis()},
        clock=fixed_clock,
        duration_ms=125,
        usage=usage,
    )

    result = provider.analyze(make_message())

    assert result.message_id == "sample-001"
    assert result.analysis == make_analysis()
    assert result.provider == "fake"
    assert result.model_name == "fake-structured-v1"
    assert result.prompt_version == "triage-v4"
    assert result.analyzed_at == fixed_clock()
    assert result.duration_ms == 125
    assert result.usage == usage
    assert result.request_id == "fake-request-0001"
    assert provider.calls == ("sample-001",)


def test_fake_provider_assigns_deterministic_request_numbers() -> None:
    provider = FakeLLMProvider(
        {
            "sample-001": make_analysis(),
            "sample-002": make_analysis(),
        },
        clock=fixed_clock,
    )

    first = provider.analyze(make_message("sample-001"))
    second = provider.analyze(make_message("sample-002"))

    assert first.request_id == "fake-request-0001"
    assert second.request_id == "fake-request-0002"
    assert provider.calls == ("sample-001", "sample-002")


def test_fake_provider_copies_response_mapping() -> None:
    responses = {"sample-001": make_analysis()}
    provider = FakeLLMProvider(responses, clock=fixed_clock)
    responses.clear()

    assert provider.analyze(make_message()).analysis == make_analysis()


def test_fake_provider_reports_missing_response() -> None:
    provider = FakeLLMProvider({}, clock=fixed_clock)

    with pytest.raises(LLMResponseNotConfiguredError) as captured:
        provider.analyze(make_message("missing-message"))

    assert captured.value.provider_name == "fake"
    assert captured.value.message_id == "missing-message"
    assert provider.calls == ("missing-message",)


def test_fake_provider_simulates_provider_failure() -> None:
    provider = FakeLLMProvider(
        {},
        failures={"sample-001": "simulated timeout"},
        clock=fixed_clock,
    )

    with pytest.raises(LLMProviderUnavailableError, match="simulated timeout"):
        provider.analyze(make_message())


def test_fake_provider_rejects_overlapping_response_and_failure() -> None:
    with pytest.raises(ValueError, match="both responses and failures"):
        FakeLLMProvider(
            {"sample-001": make_analysis()},
            failures={"sample-001": "simulated timeout"},
        )


def test_fake_provider_wraps_invalid_envelope_metadata() -> None:
    def naive_clock() -> datetime:
        return datetime(2026, 8, 7, 14, 0)

    provider = FakeLLMProvider(
        {"sample-001": make_analysis()},
        clock=naive_clock,
    )

    with pytest.raises(LLMProviderContractError) as captured:
        provider.analyze(make_message())

    assert captured.value.validation_error.error_count() == 1


def test_fake_provider_rejects_negative_duration() -> None:
    with pytest.raises(ValueError, match="duration_ms"):
        FakeLLMProvider({}, duration_ms=-1)
