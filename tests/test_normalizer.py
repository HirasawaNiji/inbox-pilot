"""Tests for deterministic email content and field normalization."""

from datetime import UTC, datetime

import pytest

from inbox_agent.models import (
    AttachmentMetadata,
    BodyType,
    EmailAddress,
    EmailMessage,
    Importance,
    MailSource,
    MessageBody,
)
from inbox_agent.normalizer import (
    extract_domain,
    html_to_text,
    normalize_email_address,
    normalize_message,
    normalize_whitespace,
    strip_standard_signature,
)


def make_message(
    *,
    body_type: BodyType = BodyType.TEXT,
    body_content: str = "请查看课程通知。",
) -> EmailMessage:
    """Build a representative message for normalizer tests."""

    return EmailMessage(
        source=MailSource.MOCK,
        source_id="sample-001",
        subject="  课程\n项目通知  ",
        from_address=EmailAddress(name="  示例 教师  ", address="Teacher@Example.EDU"),
        sender=EmailAddress(name="教务系统", address="Mailer@Example.EDU"),
        reply_to=(EmailAddress(address="Reply@Example.EDU"),),
        to_recipients=(
            EmailAddress(address="Student@Example.EDU"),
            EmailAddress(address="All-Students@Example.EDU"),
        ),
        cc_recipients=(EmailAddress(address="Assistant@Example.EDU"),),
        received_at=datetime(2026, 8, 4, 9, 30, tzinfo=UTC),
        sent_at=datetime(2026, 8, 4, 9, 25, tzinfo=UTC),
        body=MessageBody(content_type=body_type, content=body_content),
        body_preview="  请查看\n课程通知。  ",
        importance=Importance.HIGH,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  alpha\n\tbeta  ", "alpha beta"),
        ("alpha\u00a0\u00a0beta", "alpha beta"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_whitespace(value: str, expected: str) -> None:
    assert normalize_whitespace(value) == expected


def test_html_to_text_keeps_visible_content_and_decodes_entities() -> None:
    html = """
    <html>
      <head><title>Hidden title</title><style>.x { color: red; }</style></head>
      <body>
        <p>课程&nbsp;通知</p>
        <script>alert('hidden')</script>
        <noscript>hidden fallback</noscript>
        <template>hidden template</template>
        <div>请在 <strong>周五</strong> 前提交。</div>
      </body>
    </html>
    """

    text = html_to_text(html)

    assert text == "课程 通知 请在 周五 前提交。"
    assert "hidden" not in text
    assert "alert" not in text
    assert "color" not in text


def test_html_to_text_handles_empty_markup() -> None:
    assert html_to_text("<html><body><br></body></html>") == ""


def test_strip_standard_signature_uses_standalone_separator() -> None:
    value = "请在周五前提交。\n-- \n示例教师\nteacher@example.edu"

    assert strip_standard_signature(value) == "请在周五前提交。"
    assert strip_standard_signature("日期范围 8--10 月") == "日期范围 8--10 月"


def test_html_to_text_removes_standard_signature() -> None:
    html = "<p>请查看通知。</p><div>--</div><div>示例教师</div>"

    assert html_to_text(html) == "请查看通知。"


def test_address_helpers_normalize_case_and_domain() -> None:
    assert normalize_email_address(" Teacher@Example.EDU ") == "teacher@example.edu"
    assert extract_domain("Teacher@Sub.Example.EDU") == "sub.example.edu"


def test_normalize_message_cleans_text_body_and_common_fields() -> None:
    message = make_message(body_content="  第一行\n\n第二行\t结束  ")

    normalized = normalize_message(message)

    assert normalized.source is MailSource.MOCK
    assert normalized.source_id == "sample-001"
    assert normalized.subject == "课程 项目通知"
    assert normalized.from_name == "示例 教师"
    assert normalized.from_address == "teacher@example.edu"
    assert normalized.from_domain == "example.edu"
    assert normalized.sender_address == "mailer@example.edu"
    assert normalized.body_text == "第一行 第二行 结束"
    assert normalized.body_preview == "请查看 课程通知。"
    assert normalized.importance is Importance.HIGH
    assert normalized.received_at == message.received_at
    assert normalized.sent_at == message.sent_at


def test_normalize_message_removes_standard_plaintext_signature() -> None:
    message = make_message(body_content="请查看通知。\n--\n示例教师")

    normalized = normalize_message(message)

    assert normalized.body_text == "请查看通知。"


def test_normalize_message_converts_html_body() -> None:
    message = make_message(
        body_type=BodyType.HTML,
        body_content="<p>请查看 <b>附件</b>。</p><script>bad()</script>",
    )

    normalized = normalize_message(message)

    assert normalized.body_text == "请查看 附件 。"
    assert "bad" not in normalized.body_text


def test_normalize_message_normalizes_recipient_collections() -> None:
    normalized = normalize_message(make_message())

    assert normalized.reply_to_addresses == ("reply@example.edu",)
    assert normalized.to_addresses == (
        "student@example.edu",
        "all-students@example.edu",
    )
    assert normalized.cc_addresses == ("assistant@example.edu",)
    assert normalized.recipient_count == 3


def test_normalize_message_preserves_absent_transport_sender() -> None:
    message = make_message().model_copy(update={"sender": None})

    normalized = normalize_message(message)

    assert normalized.sender_address is None
    assert normalized.from_address == "teacher@example.edu"


def test_normalize_message_uses_effective_attachment_metadata() -> None:
    message = make_message().model_copy(
        update={
            "has_attachments": False,
            "attachments": (AttachmentMetadata(name="assignment.pdf", size_bytes=1024),),
        }
    )

    normalized = normalize_message(message)

    assert normalized.has_attachments is True


def test_normalize_message_does_not_modify_input() -> None:
    message = make_message()

    normalize_message(message)

    assert message.subject == "课程\n项目通知"
    assert message.from_address.address == "Teacher@Example.EDU"
