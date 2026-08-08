"""Tests for private Outlook Inbox delta synchronization."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from inbox_agent.graph import (
    GraphAccessToken,
    GraphInboxSynchronizer,
    GraphMailClient,
    GraphSettings,
)

CLIENT_ID = "12345678-1234-4234-8234-123456789abc"
BASE_URL = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"


def graph_message(message_id: str, subject: str) -> dict[str, object]:
    return {
        "@odata.etag": 'W/"example"',
        "id": message_id,
        "internetMessageId": f"<{message_id}@example.com>",
        "subject": subject,
        "from": {"emailAddress": {"name": "Example Sender", "address": "sender@example.com"}},
        "sender": {"emailAddress": {"name": "Example Sender", "address": "sender@example.com"}},
        "replyTo": [],
        "toRecipients": [{"emailAddress": {"name": "Student", "address": "student@outlook.com"}}],
        "ccRecipients": [],
        "receivedDateTime": "2026-08-08T02:00:00Z",
        "sentDateTime": "2026-08-08T01:59:00Z",
        "body": {"contentType": "html", "content": "<p>Hello</p>"},
        "bodyPreview": "Hello",
        "importance": "normal",
        "inferenceClassification": "focused",
        "hasAttachments": False,
    }


def settings() -> GraphSettings:
    return GraphSettings(client_id=CLIENT_ID)


def synchronizer(
    tmp_path: Path,
    handler: httpx.MockTransport,
) -> GraphInboxSynchronizer:
    configured = settings()
    return GraphInboxSynchronizer(
        configured,
        GraphMailClient(configured, httpx.Client(transport=handler)),
        tmp_path,
        clock=lambda: datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
    )


def test_initial_sync_follows_pages_and_writes_private_dataset_and_state(tmp_path: Path) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if len(requested_urls) == 1:
            return httpx.Response(
                200,
                json={
                    "value": [graph_message("message-1", "First")],
                    "@odata.nextLink": f"{BASE_URL}?$skiptoken=page-2",
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [graph_message("message-2", "Second")],
                "@odata.deltaLink": f"{BASE_URL}?$deltatoken=checkpoint-1",
            },
        )

    report = synchronizer(tmp_path, httpx.MockTransport(handler)).sync(GraphAccessToken("token"))

    assert report.completed is True
    assert report.started_from_delta is False
    assert report.pages_fetched == 2
    assert report.created_count == 2
    assert report.total_messages == 2
    assert "%24filter=receivedDateTime" in requested_urls[0]
    dataset = json.loads((tmp_path / "data/private/outlook_inbox.json").read_text("utf-8"))
    assert [message["source_id"] for message in dataset["messages"]] == [
        "message-1",
        "message-2",
    ]
    state = json.loads((tmp_path / "data/private/graph_sync_state.json").read_text("utf-8"))
    assert state["delta_link"].endswith("checkpoint-1")


def test_incremental_sync_reuses_delta_updates_and_removes_messages(tmp_path: Path) -> None:
    initial_transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "value": [
                    graph_message("message-1", "Original"),
                    graph_message("message-2", "Will be removed"),
                ],
                "@odata.deltaLink": f"{BASE_URL}?$deltatoken=checkpoint-1",
            },
        )
    )
    synchronizer(tmp_path, initial_transport).sync(GraphAccessToken("token"))
    requested_urls: list[str] = []

    def incremental_handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "value": [
                    graph_message("message-1", "Updated"),
                    {"id": "message-2", "@removed": {"reason": "deleted"}},
                ],
                "@odata.deltaLink": f"{BASE_URL}?$deltatoken=checkpoint-2",
            },
        )

    report = synchronizer(tmp_path, httpx.MockTransport(incremental_handler)).sync(
        GraphAccessToken("token")
    )

    assert report.started_from_delta is True
    assert report.updated_count == 1
    assert report.removed_count == 1
    assert report.total_messages == 1
    assert requested_urls == [f"{BASE_URL}?$deltatoken=checkpoint-1"]


def test_mapping_failure_keeps_previous_delta_checkpoint(tmp_path: Path) -> None:
    state_path = tmp_path / "data/private/graph_sync_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "mail_folder": "inbox",
                "delta_link": f"{BASE_URL}?$deltatoken=old",
                "synchronized_at": "2026-08-08T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "value": [{"id": "invalid-message"}],
                "@odata.deltaLink": f"{BASE_URL}?$deltatoken=new",
            },
        )
    )

    report = synchronizer(tmp_path, transport).sync(GraphAccessToken("token"))

    assert report.completed is False
    assert len(report.failures) == 1
    assert report.failures[0].message_id == "invalid-message"
    preserved = json.loads(state_path.read_text("utf-8"))
    assert preserved["delta_link"].endswith("old")
