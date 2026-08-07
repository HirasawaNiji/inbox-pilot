"""Load versioned offline responses used by the deterministic fake provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from pydantic import Field, ValidationError, model_validator

from inbox_agent.models import FrozenModel, LLMMessageAnalysis


class FakeLLMResponse(FrozenModel):
    """One message ID and its pre-authored structured LLM response."""

    source_id: str = Field(min_length=1, max_length=512)
    analysis: LLMMessageAnalysis


class FakeLLMResponseSet(FrozenModel):
    """Versioned offline response fixture for one anonymous dataset."""

    dataset_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=100,
    )
    source_dataset: str = Field(min_length=1, max_length=1_000)
    prompt_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=100,
    )
    responses: tuple[FakeLLMResponse, ...]

    @model_validator(mode="after")
    def validate_unique_source_ids(self) -> Self:
        """Require exactly one configured response per source message ID."""

        source_ids = [response.source_id for response in self.responses]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("fake LLM responses contain duplicate source IDs")
        return self

    def as_mapping(self) -> dict[str, LLMMessageAnalysis]:
        """Return the mapping accepted by ``FakeLLMProvider``."""

        return {response.source_id: response.analysis for response in self.responses}


class FakeLLMResponsesLoadError(Exception):
    """Base class for offline fake-response loading failures."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class FakeLLMResponsesNotFoundError(FakeLLMResponsesLoadError):
    """Raised when an offline response fixture is missing."""


class FakeLLMResponsesReadError(FakeLLMResponsesLoadError):
    """Raised when an offline response fixture cannot be read."""


class FakeLLMResponsesJSONError(FakeLLMResponsesLoadError):
    """Raised when an offline response fixture contains invalid JSON."""


class FakeLLMResponsesValidationError(FakeLLMResponsesLoadError):
    """Raised when an offline response fixture violates its strict schema."""

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(path, "Fake LLM responses do not match the InboxPilot schema")


def load_fake_llm_responses(path: str | Path) -> FakeLLMResponseSet:
    """Read and validate a UTF-8 offline fake-response JSON file."""

    response_path = Path(path)
    try:
        raw_content = response_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise FakeLLMResponsesNotFoundError(
            response_path,
            "Fake LLM response file does not exist",
        ) from error
    except OSError as error:
        raise FakeLLMResponsesReadError(
            response_path,
            "Unable to read fake LLM response file",
        ) from error

    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as error:
        location = f"Invalid JSON at line {error.lineno}, column {error.colno}"
        raise FakeLLMResponsesJSONError(response_path, location) from error

    try:
        return FakeLLMResponseSet.model_validate(payload)
    except ValidationError as error:
        raise FakeLLMResponsesValidationError(response_path, error) from error
