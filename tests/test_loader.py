"""Tests for loading and validating offline JSON email datasets."""

import json
from pathlib import Path

import pytest

from inbox_agent.loader import (
    DatasetDecodeError,
    DatasetJSONError,
    DatasetNotFoundError,
    DatasetReadError,
    DatasetValidationError,
    load_dataset,
)
from inbox_agent.models import BodyType, MailSource


def make_payload() -> dict[str, object]:
    """Return a minimal valid version 1.0 dataset payload."""

    return {
        "schema_version": "1.0",
        "messages": [
            {
                "source": "mock",
                "source_id": "sample-001",
                "subject": "课程项目补交通知",
                "from_address": {"name": "示例教师", "address": "teacher@example.edu"},
                "to_recipients": [{"name": "示例学生", "address": "student@example.edu"}],
                "received_at": "2026-08-04T09:30:00+08:00",
                "body": {
                    "content_type": "text",
                    "content": "请在本周五前提交课程项目。",
                },
            }
        ],
    }


def write_payload(path: Path, payload: object) -> None:
    """Write JSON without escaping the anonymous Chinese sample text."""

    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_dataset_returns_validated_models(tmp_path: Path) -> None:
    dataset_path = tmp_path / "messages.json"
    write_payload(dataset_path, make_payload())

    dataset = load_dataset(dataset_path)

    assert dataset.schema_version == "1.0"
    assert len(dataset.messages) == 1
    assert dataset.messages[0].source is MailSource.MOCK
    assert dataset.messages[0].body.content_type is BodyType.TEXT
    assert dataset.messages[0].subject == "课程项目补交通知"


def test_load_dataset_accepts_string_path(tmp_path: Path) -> None:
    dataset_path = tmp_path / "messages.json"
    write_payload(dataset_path, make_payload())

    dataset = load_dataset(str(dataset_path))

    assert dataset.messages[0].source_id == "sample-001"


def test_load_dataset_reports_missing_file(tmp_path: Path) -> None:
    dataset_path = tmp_path / "missing.json"

    with pytest.raises(DatasetNotFoundError, match="does not exist") as error:
        load_dataset(dataset_path)

    assert error.value.path == dataset_path


def test_load_dataset_rejects_non_utf8_content(tmp_path: Path) -> None:
    dataset_path = tmp_path / "messages.json"
    dataset_path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(DatasetDecodeError, match="valid UTF-8"):
        load_dataset(dataset_path)


def test_load_dataset_reports_invalid_json_location(tmp_path: Path) -> None:
    dataset_path = tmp_path / "messages.json"
    dataset_path.write_text('{"messages": [}', encoding="utf-8")

    with pytest.raises(DatasetJSONError, match=r"line 1, column \d+"):
        load_dataset(dataset_path)


def test_load_dataset_rejects_invalid_schema(tmp_path: Path) -> None:
    payload = make_payload()
    messages = payload["messages"]
    assert isinstance(messages, list)
    message = messages[0]
    assert isinstance(message, dict)
    del message["from_address"]
    dataset_path = tmp_path / "messages.json"
    write_payload(dataset_path, payload)

    with pytest.raises(DatasetValidationError) as error:
        load_dataset(dataset_path)

    assert error.value.validation_error.error_count() == 1


def test_load_dataset_rejects_duplicate_provider_identity(tmp_path: Path) -> None:
    payload = make_payload()
    messages = payload["messages"]
    assert isinstance(messages, list)
    first_message = messages[0]
    assert isinstance(first_message, dict)
    messages.append(first_message.copy())
    dataset_path = tmp_path / "messages.json"
    write_payload(dataset_path, payload)

    with pytest.raises(DatasetValidationError, match="InboxPilot schema") as error:
        load_dataset(dataset_path)

    assert "duplicate" in str(error.value.validation_error).lower()


def test_load_dataset_reports_unreadable_path(tmp_path: Path) -> None:
    with pytest.raises(DatasetReadError, match="Unable to read"):
        load_dataset(tmp_path)
