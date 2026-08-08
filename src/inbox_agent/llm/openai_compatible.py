"""Real OpenAI-compatible providers with strict, validated output."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from openai import OpenAI, OpenAIError
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import Field, ValidationError, field_validator
from yaml import YAMLError

from inbox_agent.llm.prompt import (
    CLASSIFICATION_PROMPT_VERSION,
    MAX_BODY_CHARACTER_LIMIT,
    build_classification_prompt,
)
from inbox_agent.llm.provider import (
    LLMProviderContractError,
    LLMProviderError,
    LLMProviderResponseError,
    LLMProviderUnavailableError,
)
from inbox_agent.models import (
    FrozenModel,
    LLMAnalysisResult,
    LLMMessageAnalysis,
    LLMTokenUsage,
    NormalizedMessage,
)


class OpenAICompatibleService(StrEnum):
    """Supported services that share the OpenAI chat-completions transport."""

    OPENAI = "openai"
    DEEPSEEK = "deepseek"


class OpenAICompatibleSettings(FrozenModel):
    """Public provider configuration; API keys remain outside YAML."""

    provider: OpenAICompatibleService
    model: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    base_url: str = Field(min_length=8, max_length=2_000)
    api_key_env: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    analysis_timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=255)
    max_body_characters: int = Field(default=12_000, ge=1, le=MAX_BODY_CHARACTER_LIMIT)
    max_completion_tokens: int = Field(default=2_000, ge=64, le=100_000)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    max_retries: int = Field(default=2, ge=0, le=10)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        """Require encrypted provider transport and normalize a trailing slash."""

        if not value.startswith("https://"):
            raise ValueError("base_url must use https")
        return value.rstrip("/")

    @field_validator("analysis_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Reject timezone names that cannot resolve before the first request."""

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown analysis timezone: {value}") from error
        return value


class LLMProviderConfigurationError(Exception):
    """Base class for provider configuration or credential failures."""


class LLMProviderSettingsError(LLMProviderConfigurationError):
    """Base class for failures while loading a provider YAML file."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class LLMProviderSettingsNotFoundError(LLMProviderSettingsError):
    """Raised when the provider settings file is missing."""


class LLMProviderSettingsReadError(LLMProviderSettingsError):
    """Raised when the provider settings file cannot be read."""


class LLMProviderSettingsYAMLError(LLMProviderSettingsError):
    """Raised when the provider settings file is not valid YAML."""


class LLMProviderSettingsValidationError(LLMProviderSettingsError):
    """Raised when provider YAML violates the strict settings schema."""

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(path, "LLM provider settings do not match the InboxPilot schema")


class LLMProviderCredentialError(LLMProviderConfigurationError):
    """Raised when the configured environment variable contains no API key."""

    def __init__(self, environment_variable: str) -> None:
        self.environment_variable = environment_variable
        super().__init__(
            f"API key environment variable is missing or empty: {environment_variable}"
        )


def load_openai_compatible_settings(path: str | Path) -> OpenAICompatibleSettings:
    """Read and validate one UTF-8 OpenAI-compatible provider YAML file."""

    settings_path = Path(path)
    try:
        raw_content = settings_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise LLMProviderSettingsNotFoundError(
            settings_path,
            "LLM provider settings file does not exist",
        ) from error
    except OSError as error:
        raise LLMProviderSettingsReadError(
            settings_path,
            "Unable to read LLM provider settings file",
        ) from error

    try:
        payload = yaml.safe_load(raw_content)
    except YAMLError as error:
        raise LLMProviderSettingsYAMLError(
            settings_path,
            "LLM provider settings contain invalid YAML",
        ) from error

    try:
        return OpenAICompatibleSettings.model_validate(payload)
    except ValidationError as error:
        raise LLMProviderSettingsValidationError(settings_path, error) from error


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _privacy_identifier(message_id: str) -> str:
    """Create a stable pseudonymous abuse-detection identifier."""

    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()


def _extra_value(value: object, name: str) -> object | None:
    direct = getattr(value, name, None)
    if direct is not None:
        return cast(object, direct)
    extras = getattr(value, "model_extra", None)
    if isinstance(extras, dict):
        return cast(object | None, extras.get(name))
    return None


def _nonnegative_int(value: object | None) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _token_usage(completion: ChatCompletion) -> LLMTokenUsage | None:
    usage = completion.usage
    if usage is None:
        return None

    input_tokens = _nonnegative_int(_extra_value(usage, "prompt_tokens"))
    output_tokens = _nonnegative_int(_extra_value(usage, "completion_tokens"))
    prompt_details = _extra_value(usage, "prompt_tokens_details")
    cached_tokens = _nonnegative_int(
        _extra_value(prompt_details, "cached_tokens") if prompt_details is not None else None
    )
    if cached_tokens == 0:
        cached_tokens = _nonnegative_int(_extra_value(usage, "prompt_cache_hit_tokens"))
    cached_tokens = min(cached_tokens, input_tokens)

    return LLMTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_tokens,
    )


def _safe_error_message(error: Exception) -> str:
    """Keep diagnostics bounded without serializing request headers or bodies."""

    message = str(error).replace("\r", " ").replace("\n", " ").strip()
    return (message or type(error).__name__)[:500]


class OpenAICompatibleProvider:
    """Call OpenAI Structured Outputs or DeepSeek JSON mode behind one protocol."""

    def __init__(
        self,
        settings: OpenAICompatibleSettings,
        client: OpenAI,
        *,
        clock: Callable[[], datetime] = _utc_now,
        timer: Callable[[], float] = monotonic,
    ) -> None:
        self.settings = settings
        self._client = client
        self._clock = clock
        self._timer = timer

    @classmethod
    def from_yaml(
        cls,
        path: str | Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> OpenAICompatibleProvider:
        """Build a provider from public YAML and an environment-held API key."""

        settings = load_openai_compatible_settings(path)
        values = os.environ if environment is None else environment
        api_key = values.get(settings.api_key_env, "").strip()
        if not api_key:
            raise LLMProviderCredentialError(settings.api_key_env)
        client = OpenAI(
            api_key=api_key,
            base_url=settings.base_url,
            timeout=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )
        return cls(settings, client)

    @property
    def provider_name(self) -> str:
        return self.settings.provider.value

    @property
    def model_name(self) -> str:
        return self.settings.model

    @property
    def prompt_version(self) -> str:
        return CLASSIFICATION_PROMPT_VERSION

    def _messages(self, message: NormalizedMessage) -> list[ChatCompletionMessageParam]:
        prompt = build_classification_prompt(
            message,
            analysis_timezone=self.settings.analysis_timezone,
            max_body_characters=self.settings.max_body_characters,
        )
        system_message = prompt.system_message
        if self.settings.provider is OpenAICompatibleService.DEEPSEEK:
            response_schema = json.dumps(
                prompt.response_model.model_json_schema(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            system_message = (
                f"{system_message}\n\n"
                "响应必须严格匹配下面的 JSON Schema。所有 required 字段都必须出现；"
                "nullable 字段在无值时使用 null；不得增加 Schema 之外的字段。\n"
                f"{response_schema}"
            )
        return [
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt.user_message},
        ]

    def _openai_analysis(
        self,
        message: NormalizedMessage,
        messages: list[ChatCompletionMessageParam],
    ) -> tuple[ChatCompletion, LLMMessageAnalysis]:
        completion = self._client.chat.completions.parse(
            model=self.model_name,
            messages=messages,
            response_format=LLMMessageAnalysis,
            max_completion_tokens=self.settings.max_completion_tokens,
            store=False,
            safety_identifier=_privacy_identifier(message.source_id),
        )
        if not completion.choices:
            raise LLMProviderResponseError(
                self.provider_name, message.source_id, "response contains no choices"
            )
        choice = completion.choices[0]
        if choice.finish_reason != "stop":
            raise LLMProviderResponseError(
                self.provider_name,
                message.source_id,
                f"response ended with finish_reason={choice.finish_reason}",
            )
        if choice.message.refusal:
            raise LLMProviderResponseError(
                self.provider_name, message.source_id, "model refused the classification request"
            )
        parsed = choice.message.parsed
        if not isinstance(parsed, LLMMessageAnalysis):
            raise LLMProviderResponseError(
                self.provider_name, message.source_id, "response contains no parsed analysis"
            )
        return completion, parsed

    def _deepseek_analysis(
        self,
        message: NormalizedMessage,
        messages: list[ChatCompletionMessageParam],
    ) -> tuple[ChatCompletion, LLMMessageAnalysis]:
        completion = self._client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=self.settings.max_completion_tokens,
            extra_body={"thinking": {"type": "disabled"}},
        )
        if not completion.choices:
            raise LLMProviderResponseError(
                self.provider_name, message.source_id, "response contains no choices"
            )
        choice = completion.choices[0]
        if choice.finish_reason != "stop":
            raise LLMProviderResponseError(
                self.provider_name,
                message.source_id,
                f"response ended with finish_reason={choice.finish_reason}",
            )
        content = choice.message.content
        if not content or not content.strip():
            raise LLMProviderResponseError(
                self.provider_name, message.source_id, "response contains empty JSON content"
            )
        return completion, LLMMessageAnalysis.model_validate_json(content)

    def analyze(self, message: NormalizedMessage) -> LLMAnalysisResult:
        """Analyze one message and return a provider-neutral audit envelope."""

        started_at = self._timer()
        try:
            messages = self._messages(message)
            if self.settings.provider is OpenAICompatibleService.OPENAI:
                completion, analysis = self._openai_analysis(message, messages)
            else:
                completion, analysis = self._deepseek_analysis(message, messages)
        except LLMProviderError:
            raise
        except ValidationError as error:
            raise LLMProviderContractError(self.provider_name, message.source_id, error) from error
        except OpenAIError as error:
            raise LLMProviderUnavailableError(
                self.provider_name,
                message.source_id,
                _safe_error_message(error),
            ) from error

        duration_ms = max(0, round((self._timer() - started_at) * 1_000))
        actual_model = completion.model or self.model_name
        try:
            return LLMAnalysisResult(
                message_id=message.source_id,
                analysis=analysis,
                provider=self.provider_name,
                model_name=actual_model,
                prompt_version=self.prompt_version,
                analyzed_at=self._clock(),
                duration_ms=duration_ms,
                usage=_token_usage(completion),
                request_id=completion.id or None,
            )
        except ValidationError as error:
            raise LLMProviderContractError(self.provider_name, message.source_id, error) from error
