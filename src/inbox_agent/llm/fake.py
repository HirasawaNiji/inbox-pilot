"""Deterministic offline LLM provider for tests and local development."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from pydantic import ValidationError

from inbox_agent.llm.provider import (
    LLMProviderContractError,
    LLMProviderUnavailableError,
    LLMResponseNotConfiguredError,
)
from inbox_agent.models import (
    LLMAnalysisResult,
    LLMMessageAnalysis,
    LLMTokenUsage,
    NormalizedMessage,
)


def _utc_now() -> datetime:
    """Return the current UTC time for production-like default metadata."""

    return datetime.now(UTC)


class FakeLLMProvider:
    """Return configured analyses without network, credentials, or an SDK."""

    def __init__(
        self,
        responses: Mapping[str, LLMMessageAnalysis],
        *,
        failures: Mapping[str, str] | None = None,
        provider_name: str = "fake",
        model_name: str = "fake-structured-v1",
        prompt_version: str = "triage-v4",
        clock: Callable[[], datetime] = _utc_now,
        duration_ms: int = 0,
        usage: LLMTokenUsage | None = None,
    ) -> None:
        configured_failures = dict(failures or {})
        overlap = set(responses) & set(configured_failures)
        if overlap:
            duplicate_ids = ", ".join(sorted(overlap))
            raise ValueError(
                f"fake provider IDs cannot have both responses and failures: {duplicate_ids}"
            )
        if duration_ms < 0:
            raise ValueError("duration_ms must not be negative")

        self._responses = dict(responses)
        self._failures = configured_failures
        self._provider_name = provider_name
        self._model_name = model_name
        self._prompt_version = prompt_version
        self._clock = clock
        self._duration_ms = duration_ms
        self._usage = usage
        self._calls: list[str] = []

    @property
    def provider_name(self) -> str:
        """Return the provider identifier stored in generated envelopes."""

        return self._provider_name

    @property
    def model_name(self) -> str:
        """Return the deterministic fake model identifier."""

        return self._model_name

    @property
    def prompt_version(self) -> str:
        """Return the prompt version stored in generated envelopes."""

        return self._prompt_version

    @property
    def calls(self) -> tuple[str, ...]:
        """Expose an immutable record of analyzed message IDs."""

        return tuple(self._calls)

    def analyze(self, message: NormalizedMessage) -> LLMAnalysisResult:
        """Return one configured response or a typed deterministic failure."""

        message_id = message.source_id
        self._calls.append(message_id)

        failure_message = self._failures.get(message_id)
        if failure_message is not None:
            raise LLMProviderUnavailableError(
                self.provider_name,
                message_id,
                failure_message,
            )

        analysis = self._responses.get(message_id)
        if analysis is None:
            raise LLMResponseNotConfiguredError(
                self.provider_name,
                message_id,
                "no offline response is configured",
            )

        request_number = len(self._calls)
        try:
            return LLMAnalysisResult(
                message_id=message_id,
                analysis=analysis,
                provider=self.provider_name,
                model_name=self.model_name,
                prompt_version=self.prompt_version,
                analyzed_at=self._clock(),
                duration_ms=self._duration_ms,
                usage=self._usage,
                request_id=f"fake-request-{request_number:04d}",
            )
        except ValidationError as error:
            raise LLMProviderContractError(
                self.provider_name,
                message_id,
                error,
            ) from error
