"""Convert Microsoft Graph message JSON into InboxPilot's provider-neutral model."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import Field

from inbox_agent.models import (
    BodyType,
    EmailAddress,
    EmailMessage,
    FrozenModel,
    Importance,
    MailSource,
    MessageBody,
)


class GraphEmailAddressPayload(FrozenModel):
    name: str = Field(default="", max_length=320)
    address: str = Field(min_length=3, max_length=320)


class GraphRecipientPayload(FrozenModel):
    email_address: GraphEmailAddressPayload = Field(alias="emailAddress")


class GraphBodyPayload(FrozenModel):
    content_type: Literal["text", "html"] = Field(alias="contentType")
    content: str = Field(default="", max_length=1_000_000)


class GraphMessagePayload(FrozenModel):
    id: str = Field(min_length=1, max_length=512)
    internet_message_id: str | None = Field(default=None, alias="internetMessageId")
    subject: str = Field(default="", max_length=2_000)
    from_recipient: GraphRecipientPayload = Field(alias="from")
    sender: GraphRecipientPayload | None = None
    reply_to: tuple[GraphRecipientPayload, ...] = Field(default=(), alias="replyTo")
    to_recipients: tuple[GraphRecipientPayload, ...] = Field(default=(), alias="toRecipients")
    cc_recipients: tuple[GraphRecipientPayload, ...] = Field(default=(), alias="ccRecipients")
    received_at: datetime = Field(alias="receivedDateTime")
    sent_at: datetime | None = Field(default=None, alias="sentDateTime")
    body: GraphBodyPayload
    body_preview: str = Field(default="", alias="bodyPreview", max_length=5_000)
    importance: Importance = Importance.NORMAL
    inference_classification: Literal["focused", "other"] | None = Field(
        default=None,
        alias="inferenceClassification",
    )
    categories: tuple[str, ...] = Field(default=(), max_length=100)
    change_key: str | None = Field(default=None, alias="changeKey", min_length=1, max_length=512)
    has_attachments: bool = Field(default=False, alias="hasAttachments")


def _address(payload: GraphRecipientPayload) -> EmailAddress:
    return EmailAddress(
        name=payload.email_address.name,
        address=payload.email_address.address,
    )


def map_graph_message(payload: object) -> EmailMessage:
    """Validate one Graph object and map it without downloading attachments."""

    sanitized = payload
    if isinstance(payload, Mapping):
        sanitized = {
            key: value
            for key, value in payload.items()
            if isinstance(key, str) and not key.startswith("@odata.")
        }
    message = GraphMessagePayload.model_validate(sanitized)
    return EmailMessage(
        source=MailSource.MICROSOFT_GRAPH,
        source_id=message.id,
        internet_message_id=message.internet_message_id,
        subject=message.subject,
        from_address=_address(message.from_recipient),
        sender=_address(message.sender) if message.sender is not None else None,
        reply_to=tuple(_address(recipient) for recipient in message.reply_to),
        to_recipients=tuple(_address(recipient) for recipient in message.to_recipients),
        cc_recipients=tuple(_address(recipient) for recipient in message.cc_recipients),
        received_at=message.received_at,
        sent_at=message.sent_at,
        body=MessageBody(
            content_type=BodyType(message.body.content_type),
            content=message.body.content,
        ),
        body_preview=message.body_preview,
        importance=message.importance,
        inference_classification=message.inference_classification,
        categories=message.categories,
        change_key=message.change_key,
        has_attachments=message.has_attachments,
    )
