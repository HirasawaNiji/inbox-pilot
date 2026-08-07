"""Load and validate offline email datasets from JSON files."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from inbox_agent.models import MessageDataset


class DatasetLoadError(Exception):
    """Base class for errors raised while loading an email dataset."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class DatasetNotFoundError(DatasetLoadError):
    """Raised when the requested dataset file does not exist."""


class DatasetReadError(DatasetLoadError):
    """Raised when the dataset cannot be read from disk."""


class DatasetDecodeError(DatasetLoadError):
    """Raised when a dataset is not valid UTF-8."""


class DatasetJSONError(DatasetLoadError):
    """Raised when a dataset does not contain valid JSON."""


class DatasetValidationError(DatasetLoadError):
    """Raised when JSON data does not match the dataset schema."""

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(path, "Dataset does not match the InboxPilot schema")


def load_dataset(path: str | Path) -> MessageDataset:
    """Read a UTF-8 JSON file and validate it as a MessageDataset."""

    dataset_path = Path(path)

    try:
        raw_content = dataset_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise DatasetNotFoundError(dataset_path, "Dataset file does not exist") from error
    except UnicodeDecodeError as error:
        raise DatasetDecodeError(dataset_path, "Dataset file is not valid UTF-8") from error
    except OSError as error:
        raise DatasetReadError(dataset_path, "Unable to read dataset file") from error

    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as error:
        location = f"Invalid JSON at line {error.lineno}, column {error.colno}"
        raise DatasetJSONError(dataset_path, location) from error

    try:
        return MessageDataset.model_validate(payload)
    except ValidationError as error:
        raise DatasetValidationError(dataset_path, error) from error
