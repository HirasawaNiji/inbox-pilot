"""Tests for the allowlisted Microsoft Graph HTTP boundary."""

from __future__ import annotations

import httpx
import pytest

from inbox_agent.graph import (
    GraphAccessToken,
    GraphAuthorizationError,
    GraphMailClient,
    GraphServiceError,
    GraphSettings,
    GraphThrottledError,
    GraphURLRejectedError,
)

CLIENT_ID = "12345678-1234-4234-8234-123456789abc"
DELTA_URL = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta"
CANONICAL_DELTA_URL = (
    "https://graph.microsoft.com/v1.0/me/mailFolders('AQMkADNkNAAAgEMAAAA')/messages/delta"
)


def client(handler: httpx.MockTransport) -> GraphMailClient:
    return GraphMailClient(
        GraphSettings(client_id=CLIENT_ID),
        httpx.Client(transport=handler),
    )


def token() -> GraphAccessToken:
    return GraphAccessToken("secret-token")


def test_client_fetches_delta_page_with_read_only_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "value": [{"id": "message-1"}],
                "@odata.nextLink": f"{CANONICAL_DELTA_URL}?$skiptoken=opaque",
            },
        )

    page = client(httpx.MockTransport(handler)).get_delta_page(DELTA_URL, token())

    assert page.values == ({"id": "message-1"},)
    assert page.next_link is not None
    assert page.delta_link is None
    assert requests[0].method == "GET"
    assert requests[0].headers["Authorization"] == "Bearer secret-token"
    assert 'IdType="ImmutableId"' in requests[0].headers["Prefer"]
    assert "odata.maxpagesize=50" in requests[0].headers["Prefer"]


def test_client_accepts_graph_canonical_odata_delta_link() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={"value": [], "@odata.deltaLink": f"{CANONICAL_DELTA_URL}?$deltatoken=x"},
        )
    )

    page = client(transport).get_delta_page(CANONICAL_DELTA_URL, token())

    assert page.next_link is None
    assert page.delta_link == f"{CANONICAL_DELTA_URL}?$deltatoken=x"


@pytest.mark.parametrize(
    "url",
    [
        "http://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta",
        "https://evil.example/v1.0/me/mailFolders/inbox/messages/delta",
        "https://graph.microsoft.com/v1.0/me/messages",
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/deltaevil",
        "https://graph.microsoft.com/v1.0/me/mailFolders('inbox')/messages",
        "https://graph.microsoft.com/v1.0/me/mailFolders('inbox/escape')/messages/delta",
        "https://user@graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta",
    ],
)
def test_client_rejects_urls_outside_inbox_delta_allowlist(url: str) -> None:
    with pytest.raises(GraphURLRejectedError):
        client(httpx.MockTransport(lambda _: httpx.Response(200))).get_delta_page(url, token())


@pytest.mark.parametrize("status", [401, 403])
def test_client_reports_authorization_failures(status: int) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            status,
            json={"error": {"code": "InvalidAuthenticationToken", "message": "Denied"}},
        )
    )
    with pytest.raises(GraphAuthorizationError, match="InvalidAuthenticationToken"):
        client(transport).get_delta_page(DELTA_URL, token())


def test_client_reports_throttling_with_retry_after() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(429, headers={"Retry-After": "12"}, json={"error": {}})
    )
    with pytest.raises(GraphThrottledError) as captured:
        client(transport).get_delta_page(DELTA_URL, token())
    assert captured.value.retry_after_seconds == 12


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"value": ["not-an-object"], "@odata.deltaLink": f"{DELTA_URL}?token=x"},
        {"value": [], "@odata.nextLink": "x", "@odata.deltaLink": "y"},
        {"value": []},
    ],
)
def test_client_rejects_malformed_graph_payloads(payload: object) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(GraphServiceError):
        client(transport).get_delta_page(DELTA_URL, token())
