"""Allowlisted read-only HTTP boundary for Microsoft Graph mail delta pages."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, cast
from urllib.parse import urlsplit

import httpx

from inbox_agent.graph.auth import GraphAccessToken
from inbox_agent.graph.config import GraphSettings

GRAPH_MESSAGE_ID_TYPE: Final[Literal["restImmutableEntryId"]] = "restImmutableEntryId"
GRAPH_IMMUTABLE_ID_PREFER: Final = 'IdType="ImmutableId"'


@dataclass(frozen=True, slots=True)
class GraphDeltaPage:
    """One validated page of Graph message changes and continuation links."""

    values: tuple[Mapping[str, object], ...]
    next_link: str | None
    delta_link: str | None


class GraphRequestError(Exception):
    """Base class for safe Microsoft Graph transport and response failures."""


class GraphURLRejectedError(GraphRequestError):
    """Raised when a continuation URL escapes the read-only mail allowlist."""


class GraphAuthorizationError(GraphRequestError):
    """Raised when Graph rejects the delegated token or required permission."""


class GraphThrottledError(GraphRequestError):
    """Raised when Graph asks the client to delay requests."""

    def __init__(self, retry_after_seconds: int | None) -> None:
        self.retry_after_seconds = retry_after_seconds
        suffix = f"; retry after {retry_after_seconds}s" if retry_after_seconds is not None else ""
        super().__init__(f"Microsoft Graph throttled the request{suffix}")


class GraphServiceError(GraphRequestError):
    """Raised for network, server, or malformed Graph responses."""


def _safe_graph_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            safe_code = code if isinstance(code, str) else "GraphError"
            safe_message = message if isinstance(message, str) else "Request failed"
            return f"{safe_code}: {safe_message}".replace("\r", " ").replace("\n", " ")[:500]
    return f"HTTP {response.status_code}"


def _optional_link(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise GraphServiceError(f"Microsoft Graph returned invalid {key}")
    return value


class GraphMailClient:
    """Issue only GET requests for the signed-in user's mail-folder deltas."""

    def __init__(self, settings: GraphSettings, http_client: httpx.Client) -> None:
        self.settings = settings
        self._http_client = http_client

    def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        # Graph accepts the slash form for the initial well-known Inbox name,
        # then may canonicalize continuation links to OData key syntax:
        # /mailFolders('opaque-folder-id')/messages/delta.
        folder_selector = r"(?:/[^/]+|[(]'[^/'()]+'[)])"
        allowed_delta_path = rf"/v1[.]0/me/mailFolders{folder_selector}/messages/delta"
        if (
            parsed.scheme != "https"
            or parsed.netloc.casefold() != "graph.microsoft.com"
            or parsed.username is not None
            or parsed.password is not None
            or re.fullmatch(allowed_delta_path, parsed.path, flags=re.IGNORECASE) is None
        ):
            raise GraphURLRejectedError("Graph URL is outside the read-only Inbox delta allowlist")

    def get_delta_page(self, url: str, token: GraphAccessToken) -> GraphDeltaPage:
        """Fetch and validate one page without following arbitrary continuation hosts."""

        self._validate_url(url)
        headers = {
            "Authorization": f"Bearer {token.access_token}",
            "Accept": "application/json",
            "Prefer": (f"{GRAPH_IMMUTABLE_ID_PREFER}, odata.maxpagesize={self.settings.page_size}"),
        }
        try:
            response = self._http_client.get(
                url,
                headers=headers,
                timeout=self.settings.request_timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise GraphServiceError(
                f"Microsoft Graph network request failed: {type(error).__name__}"
            ) from error

        if response.status_code in (401, 403):
            raise GraphAuthorizationError(_safe_graph_error(response))
        if response.status_code == 429:
            raw_retry_after = response.headers.get("Retry-After")
            retry_after = None
            if raw_retry_after and raw_retry_after.isdigit():
                retry_after = int(raw_retry_after)
            raise GraphThrottledError(retry_after)
        if response.status_code >= 400:
            raise GraphServiceError(_safe_graph_error(response))

        try:
            payload = response.json()
        except ValueError as error:
            raise GraphServiceError("Microsoft Graph returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise GraphServiceError("Microsoft Graph response must be a JSON object")

        raw_values = payload.get("value")
        if not isinstance(raw_values, list):
            raise GraphServiceError("Microsoft Graph response is missing a value array")
        values: list[Mapping[str, object]] = []
        for item in raw_values:
            if not isinstance(item, dict):
                raise GraphServiceError("Microsoft Graph value entries must be JSON objects")
            values.append(cast(Mapping[str, object], item))

        next_link = _optional_link(payload, "@odata.nextLink")
        delta_link = _optional_link(payload, "@odata.deltaLink")
        if next_link is not None and delta_link is not None:
            raise GraphServiceError("Microsoft Graph returned both nextLink and deltaLink")
        if next_link is None and delta_link is None:
            raise GraphServiceError("Microsoft Graph returned no continuation or delta link")
        if next_link is not None:
            self._validate_url(next_link)
        if delta_link is not None:
            self._validate_url(delta_link)

        return GraphDeltaPage(tuple(values), next_link, delta_link)
