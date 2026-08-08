"""Provider-neutral interface and error boundary for LLM analysis."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from inbox_agent.models import LLMAnalysisResult, NormalizedMessage


class LLMProviderError(Exception):
    """Base class for failures produced by an LLM provider adapter."""

    def __init__(self, provider_name: str, message_id: str, message: str) -> None:
        self.provider_name = provider_name
        self.message_id = message_id
        super().__init__(f"{provider_name} failed for {message_id}: {message}")


class LLMResponseNotConfiguredError(LLMProviderError):
    """Raised when an offline provider has no response for a message."""


class LLMProviderUnavailableError(LLMProviderError):
    """Raised when a provider cannot complete a request."""


class LLMProviderResponseError(LLMProviderError):
    """Raised when a provider returns no usable structured response."""


class LLMProviderContractError(LLMProviderError):
    """Raised when provider output violates the structured result contract."""

    def __init__(
        self,
        provider_name: str,
        message_id: str,
        validation_error: ValidationError,
    ) -> None:
        self.validation_error = validation_error
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in validation_error.errors(include_url=False)[:5]
        )
        message = "response failed schema validation"
        if details:
            message = f"{message} ({details})"
        super().__init__(provider_name, message_id, message)


class LLMProviderResultMismatchError(LLMProviderError):
    """Raised when a provider returns a result for a different message ID."""


@runtime_checkable
class LLMProvider(Protocol):
    """Replaceable synchronous interface consumed by the triage pipeline."""

    @property
    def provider_name(self) -> str:
        """Return a stable provider identifier used in audit records."""

        ...

    @property
    def model_name(self) -> str:
        """Return the configured model or deterministic fake identifier."""

        ...

    @property
    def prompt_version(self) -> str:
        """Return the version of the structured-analysis prompt."""

        ...

    def analyze(self, message: NormalizedMessage) -> LLMAnalysisResult:
        """Analyze one normalized message or raise a typed provider error."""

        ...
