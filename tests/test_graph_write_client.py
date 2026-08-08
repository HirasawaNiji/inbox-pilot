"""Tests for the category-only Microsoft Graph write boundary."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from inbox_agent.graph import (
    GRAPH_MESSAGE_ENDPOINT,
    GraphAccessToken,
    GraphAuthorizationError,
    GraphCategoryWriteClient,
    GraphCategoryWriteRequest,
    GraphMessageCategorySnapshot,
    GraphServiceError,
    GraphThrottledError,
    GraphWriteConflictError,
    GraphWriteDisabledError,
    GraphWriteOutcomeUnknownError,
    GraphWriteRedirectRejectedError,
    GraphWriteSettings,
)

CLIENT_ID = "12345678-1234-4234-8234-123456789abc"
MESSAGE_ID = "AAMkAGI2+/="
CATEGORIES = ("School", "InboxPilot/P2", "InboxPilot/course_notice")


def settings(*, enabled: bool = True) -> GraphWriteSettings:
    return GraphWriteSettings(client_id=CLIENT_ID, write_enabled=enabled)


def request(
    *,
    message_id: str = MESSAGE_ID,
    categories: tuple[str, ...] = CATEGORIES,
) -> GraphCategoryWriteRequest:
    return GraphCategoryWriteRequest(message_id=message_id, categories=categories)


def token() -> GraphAccessToken:
    return GraphAccessToken("secret-write-token")


def write_client(
    handler: httpx.MockTransport,
    *,
    enabled: bool = True,
    follow_redirects: bool = False,
) -> GraphCategoryWriteClient:
    return GraphCategoryWriteClient(
        settings(enabled=enabled),
        httpx.Client(transport=handler, follow_redirects=follow_redirects),
    )


def response_payload(
    *,
    message_id: str = MESSAGE_ID,
    categories: tuple[str, ...] = CATEGORIES,
) -> dict[str, object]:
    return {
        "id": message_id,
        "categories": list(categories),
        "changeKey": "new-change-key",
        "subject": "must be ignored",
    }


def test_category_write_sends_one_allowlisted_patch_with_only_categories() -> None:
    requests: list[httpx.Request] = []

    def handler(graph_request: httpx.Request) -> httpx.Response:
        requests.append(graph_request)
        return httpx.Response(200, json=response_payload())

    result = write_client(httpx.MockTransport(handler)).set_categories(request(), token())

    assert result.message_id == MESSAGE_ID
    assert result.message_id_type == "restImmutableEntryId"
    assert result.categories == CATEGORIES
    assert result.change_key == "new-change-key"
    assert len(requests) == 1
    sent = requests[0]
    assert sent.method == "PATCH"
    assert str(sent.url) == f"{GRAPH_MESSAGE_ENDPOINT}/AAMkAGI2%2B%2F%3D"
    assert sent.headers["Authorization"] == "Bearer secret-write-token"
    assert sent.headers["Content-Type"].startswith("application/json")
    assert sent.headers["Prefer"] == 'IdType="ImmutableId"'
    assert json.loads(sent.content) == {"categories": list(CATEGORIES)}


def test_preflight_reads_only_id_categories_and_change_key() -> None:
    requests: list[httpx.Request] = []

    def handler(graph_request: httpx.Request) -> httpx.Response:
        requests.append(graph_request)
        return httpx.Response(200, json=response_payload())

    snapshot = write_client(httpx.MockTransport(handler)).get_category_snapshot(
        MESSAGE_ID,
        token(),
    )

    assert snapshot == GraphMessageCategorySnapshot(
        message_id=MESSAGE_ID,
        categories=CATEGORIES,
        change_key="new-change-key",
    )
    assert len(requests) == 1
    sent = requests[0]
    assert sent.method == "GET"
    assert sent.url.path.endswith("/AAMkAGI2+/=")
    assert sent.url.params["$select"] == "id,categories,changeKey"
    assert sent.headers["Authorization"] == "Bearer secret-write-token"
    assert sent.headers["Prefer"] == 'IdType="ImmutableId"'
    assert sent.content == b""


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"id": MESSAGE_ID, "categories": CATEGORIES},
        {"id": "different", "categories": CATEGORIES, "changeKey": "key"},
        {"id": MESSAGE_ID, "categories": [1], "changeKey": "key"},
    ],
)
def test_preflight_rejects_unverifiable_responses(payload: object) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))

    with pytest.raises(GraphServiceError):
        write_client(transport).get_category_snapshot(MESSAGE_ID, token())


def test_preflight_network_failure_is_a_read_error_not_unknown_write() -> None:
    def handler(graph_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("lost", request=graph_request)

    with pytest.raises(GraphServiceError, match="preflight read failed"):
        write_client(httpx.MockTransport(handler)).get_category_snapshot(MESSAGE_ID, token())


def test_category_write_encodes_an_opaque_id_as_one_path_segment() -> None:
    opaque_id = "../../users?danger=true"
    seen_urls: list[str] = []

    def handler(graph_request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(graph_request.url))
        return httpx.Response(200, json=response_payload(message_id=opaque_id))

    result = write_client(httpx.MockTransport(handler)).set_categories(
        request(message_id=opaque_id),
        token(),
    )

    assert result.message_id == opaque_id
    assert seen_urls == [f"{GRAPH_MESSAGE_ENDPOINT}/..%2F..%2Fusers%3Fdanger%3Dtrue"]


def test_category_write_requires_explicit_write_enablement() -> None:
    with pytest.raises(GraphWriteDisabledError, match="write_enabled: true"):
        write_client(
            httpx.MockTransport(lambda _: httpx.Response(200)),
            enabled=False,
        )


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"message_id": MESSAGE_ID, "message_id_type": "restId", "categories": []}, "Input"),
        ({"message_id": " message ", "categories": []}, "whitespace"),
        ({"message_id": "message\nheader", "categories": []}, "control"),
        ({"message_id": MESSAGE_ID, "categories": ["School", "school"]}, "unique"),
        ({"message_id": MESSAGE_ID, "categories": [" "]}, "empty"),
        ({"message_id": MESSAGE_ID, "categories": ["x" * 256]}, "255"),
        ({"message_id": MESSAGE_ID, "categories": [], "subject": "forbidden"}, "Extra"),
    ],
)
def test_category_write_request_rejects_ambiguous_inputs(
    payload: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValidationError, match=match):
        GraphCategoryWriteRequest.model_validate(payload)


@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 307, 308])
def test_category_write_never_follows_redirects_even_if_http_client_does(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(graph_request: httpx.Request) -> httpx.Response:
        requests.append(graph_request)
        return httpx.Response(status, headers={"Location": "https://evil.example/steal"})

    client = write_client(httpx.MockTransport(handler), follow_redirects=True)

    with pytest.raises(GraphWriteRedirectRejectedError):
        client.set_categories(request(), token())
    assert len(requests) == 1
    assert requests[0].url.host == "graph.microsoft.com"


@pytest.mark.parametrize("status", [401, 403])
def test_category_write_reports_authorization_failures(status: int) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            status,
            json={"error": {"code": "ErrorAccessDenied", "message": "Denied"}},
        )
    )

    with pytest.raises(GraphAuthorizationError, match="ErrorAccessDenied"):
        write_client(transport).set_categories(request(), token())


def test_category_write_reports_throttling_without_retrying() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "9"}, json={"error": {}})

    with pytest.raises(GraphThrottledError) as captured:
        write_client(httpx.MockTransport(handler)).set_categories(request(), token())
    assert captured.value.retry_after_seconds == 9
    assert calls == 1


@pytest.mark.parametrize("status", [409, 412])
def test_category_write_exposes_conflicts_for_a_future_executor(status: int) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            status,
            json={"error": {"code": "ErrorConflict", "message": "Changed"}},
        )
    )

    with pytest.raises(GraphWriteConflictError, match="ErrorConflict"):
        write_client(transport).set_categories(request(), token())


def test_category_write_reports_graph_service_errors_without_token_leakage() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            500,
            json={"error": {"code": "ServerError", "message": "Unavailable"}},
        )
    )

    with pytest.raises(GraphServiceError) as captured:
        write_client(transport).set_categories(request(), token())
    assert "ServerError" in str(captured.value)
    assert "secret-write-token" not in str(captured.value)


def test_category_write_treats_network_failure_as_unknown_outcome() -> None:
    def handler(graph_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection lost", request=graph_request)

    with pytest.raises(GraphWriteOutcomeUnknownError, match="outcome is unknown"):
        write_client(httpx.MockTransport(handler)).set_categories(request(), token())


@pytest.mark.parametrize("status", [201, 202, 204])
def test_category_write_rejects_unexpected_success_status_as_unknown(status: int) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(status))

    with pytest.raises(GraphWriteOutcomeUnknownError, match="unexpected write status"):
        write_client(transport).set_categories(request(), token())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"id": MESSAGE_ID, "categories": CATEGORIES},
        {"id": MESSAGE_ID, "categories": [1], "changeKey": "new-key"},
        {"id": MESSAGE_ID, "categories": ["School", "school"], "changeKey": "new-key"},
    ],
)
def test_category_write_rejects_malformed_success_response(payload: object) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))

    with pytest.raises(GraphWriteOutcomeUnknownError):
        write_client(transport).set_categories(request(), token())


@pytest.mark.parametrize(
    "payload",
    [
        response_payload(message_id="different-message"),
        response_payload(categories=("School", "InboxPilot/P1")),
    ],
)
def test_category_write_requires_response_to_match_requested_state(
    payload: dict[str, object],
) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))

    with pytest.raises(GraphWriteOutcomeUnknownError, match="could not be verified"):
        write_client(transport).set_categories(request(), token())


def test_category_write_rejects_invalid_json_after_success() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"not-json", headers={"Content-Type": "text/plain"})
    )

    with pytest.raises(GraphWriteOutcomeUnknownError, match="invalid JSON"):
        write_client(transport).set_categories(request(), token())
