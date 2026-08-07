"""Deterministically normalize provider-neutral email messages."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from inbox_agent.models import BodyType, EmailAddress, EmailMessage, NormalizedMessage

_WHITESPACE_PATTERN = re.compile(r"\s+")
_SIGNATURE_SEPARATOR_PATTERN = re.compile(r"(?m)^[ \t]*--[ \t]*$")
_NON_VISIBLE_HTML_ELEMENTS = ("head", "script", "style", "noscript", "template")


def normalize_whitespace(value: str) -> str:
    """Collapse Unicode whitespace runs and remove outer whitespace."""

    return _WHITESPACE_PATTERN.sub(" ", value).strip()


def strip_standard_signature(value: str) -> str:
    """Remove content after the conventional standalone ``--`` separator."""

    separator = _SIGNATURE_SEPARATOR_PATTERN.search(value)
    if separator is None:
        return value
    return value[: separator.start()].rstrip()


def html_to_text(value: str) -> str:
    """Convert HTML email content to compact visible text.

    Elements that cannot contribute user-visible mail content are removed with
    their children before text extraction. Beautiful Soup also resolves common
    HTML character references while parsing.
    """

    soup = BeautifulSoup(value, "html.parser")
    for element in soup.find_all(list(_NON_VISIBLE_HTML_ELEMENTS)):
        element.decompose()
    visible_text = soup.get_text(separator="\n")
    return normalize_whitespace(strip_standard_signature(visible_text))


def normalize_email_address(value: str) -> str:
    """Return the canonical lowercase form used by InboxPilot policies."""

    return value.strip().lower()


def extract_domain(address: str) -> str:
    """Extract a lowercase domain from an already validated email address."""

    return normalize_email_address(address).rsplit("@", maxsplit=1)[1]


def _normalize_addresses(addresses: tuple[EmailAddress, ...]) -> tuple[str, ...]:
    """Normalize a tuple of EmailAddress-like model values."""

    return tuple(normalize_email_address(address.address) for address in addresses)


def normalize_message(message: EmailMessage) -> NormalizedMessage:
    """Convert a validated provider message into the pipeline's clean model."""

    if message.body.content_type is BodyType.HTML:
        body_text = html_to_text(message.body.content)
    else:
        body_text = normalize_whitespace(strip_standard_signature(message.body.content))

    from_address = normalize_email_address(message.from_address.address)
    sender_address = (
        normalize_email_address(message.sender.address) if message.sender is not None else None
    )

    return NormalizedMessage(
        source=message.source,
        source_id=message.source_id,
        subject=normalize_whitespace(message.subject),
        from_name=normalize_whitespace(message.from_address.name),
        from_address=from_address,
        from_domain=extract_domain(from_address),
        sender_address=sender_address,
        reply_to_addresses=_normalize_addresses(message.reply_to),
        to_addresses=_normalize_addresses(message.to_recipients),
        cc_addresses=_normalize_addresses(message.cc_recipients),
        received_at=message.received_at,
        sent_at=message.sent_at,
        body_text=body_text,
        body_preview=normalize_whitespace(message.body_preview),
        importance=message.importance,
        has_attachments=message.effective_has_attachments,
    )
