"""Tests for mapping Microsoft Graph messages into InboxPilot models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from inbox_agent.graph import map_graph_message
from inbox_agent.models import BodyType, MailSource


def graph_message() -> dict[str, object]:
    return {
        "id": "immutable-message-001",
        "internetMessageId": "<sample-001@example.com>",
        "subject": "Personal Outlook test",
        "from": {"emailAddress": {"name": "Example Sender", "address": "sender@example.com"}},
        "sender": {"emailAddress": {"name": "Example Sender", "address": "sender@example.com"}},
        "replyTo": [{"emailAddress": {"name": "Reply Desk", "address": "reply@example.com"}}],
        "toRecipients": [{"emailAddress": {"name": "Student", "address": "student@outlook.com"}}],
        "ccRecipients": [],
        "receivedDateTime": "2026-08-08T02:00:00Z",
        "sentDateTime": "2026-08-08T01:59:00Z",
        "body": {"contentType": "html", "content": "<p>Hello</p>"},
        "bodyPreview": "Hello",
        "importance": "high",
        "inferenceClassification": "focused",
        "categories": ["School", "Important"],
        "changeKey": "change-key-001",
        "hasAttachments": True,
    }


def test_mapper_builds_provider_neutral_email_message() -> None:
    message = map_graph_message(graph_message())

    assert message.source is MailSource.MICROSOFT_GRAPH
    assert message.source_id == "immutable-message-001"
    assert message.from_address.address == "sender@example.com"
    assert message.reply_to[0].address == "reply@example.com"
    assert message.received_at == datetime(2026, 8, 8, 2, 0, tzinfo=UTC)
    assert message.body.content_type is BodyType.HTML
    assert message.body.content == "<p>Hello</p>"
    assert message.has_attachments is True
    assert message.attachments == ()
    assert message.categories == ("School", "Important")
    assert message.change_key == "change-key-001"


def test_mapper_rejects_missing_sender_and_unknown_fields() -> None:
    missing_from = graph_message()
    del missing_from["from"]
    with pytest.raises(ValidationError):
        map_graph_message(missing_from)

    extra = graph_message()
    extra["unexpected"] = "value"
    with pytest.raises(ValidationError):
        map_graph_message(extra)


def test_mapper_rejects_naive_or_invalid_graph_values() -> None:
    invalid = graph_message()
    invalid["receivedDateTime"] = "2026-08-08T02:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        map_graph_message(invalid)

    invalid = graph_message()
    invalid["body"] = {"contentType": "mime", "content": "raw"}
    with pytest.raises(ValidationError):
        map_graph_message(invalid)
