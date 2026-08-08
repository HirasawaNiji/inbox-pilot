"""Strict Microsoft Graph boundary for one message category update."""

from __future__ import annotations

import unicodedata
from typing import Literal, Self
from urllib.parse import quote

import httpx
from pydantic import Field, field_validator, model_validator

from inbox_agent.graph.auth import GraphAccessToken
from inbox_agent.graph.client import (
    GRAPH_IMMUTABLE_ID_PREFER,
    GRAPH_MESSAGE_ID_TYPE,
    GraphAuthorizationError,
    GraphRequestError,
    GraphServiceError,
    GraphThrottledError,
    _safe_graph_error,
)
from inbox_agent.graph.config import GraphWriteSettings
from inbox_agent.models import FrozenModel

GRAPH_MESSAGE_ENDPOINT = "https://graph.microsoft.com/v1.0/me/messages"


def _normalize_categories(categories: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(category.strip() for category in categories)
    if any(not category for category in normalized):
        raise ValueError("Graph category names must not be empty")
    if any(len(category) > 255 for category in normalized):
        raise ValueError("Graph category names must not exceed 255 characters")
    folded = [category.casefold() for category in normalized]
    if len(folded) != len(set(folded)):
        raise ValueError("Graph category names must be unique ignoring case")
    return normalized


class GraphCategoryWriteRequest(FrozenModel):
    """Validated allowlisted payload for one immutable Outlook message ID."""

    message_id: str = Field(min_length=1, max_length=512)
    message_id_type: Literal["restImmutableEntryId"] = GRAPH_MESSAGE_ID_TYPE
    categories: tuple[str, ...] = Field(max_length=103)

    @field_validator("message_id", mode="before")
    @classmethod
    def validate_message_id(cls, value: object) -> object:
        """Reject whitespace/control ambiguity while preserving the opaque Graph ID."""

        if isinstance(value, str):
            if value != value.strip():
                raise ValueError("Graph message ID must not contain surrounding whitespace")
            if any(unicodedata.category(character) == "Cc" for character in value):
                raise ValueError("Graph message ID must not contain control characters")
        return value

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Normalize the only property this client is allowed to write."""

        return _normalize_categories(value)


class GraphCategoryWriteResult(FrozenModel):
    """Verified Graph response after a category-only PATCH."""

    message_id: str = Field(min_length=1, max_length=512)
    message_id_type: Literal["restImmutableEntryId"] = GRAPH_MESSAGE_ID_TYPE
    categories: tuple[str, ...] = Field(max_length=103)
    change_key: str = Field(min_length=1, max_length=512)
    status_code: Literal[200] = 200

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_categories(value)

    @model_validator(mode="after")
    def validate_message_id(self) -> Self:
        if any(unicodedata.category(character) == "Cc" for character in self.message_id):
            raise ValueError("Graph response message ID contains control characters")
        return self


class GraphMessageCategorySnapshot(FrozenModel):
    """Verified live category state read immediately before a write."""

    message_id: str = Field(min_length=1, max_length=512)
    message_id_type: Literal["restImmutableEntryId"] = GRAPH_MESSAGE_ID_TYPE
    categories: tuple[str, ...] = Field(max_length=103)
    change_key: str = Field(min_length=1, max_length=512)

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_categories(value)


class GraphWriteConflictError(GraphRequestError):
    """Raised when Graph rejects a write due to a resource conflict."""


class GraphWriteRedirectRejectedError(GraphRequestError):
    """Raised when Graph attempts to redirect a credentialed write request."""


class GraphWriteOutcomeUnknownError(GraphRequestError):
    """Raised when a write may have occurred but its result cannot be verified."""


class GraphCategoryWriteClient:
    """PATCH only the categories property of one immutable message resource."""

    def __init__(self, settings: GraphWriteSettings, http_client: httpx.Client) -> None:
        settings.require_enabled()
        self.settings = settings
        self._http_client = http_client

    @staticmethod
    def _message_url(message_id: str) -> str:
        encoded_id = quote(message_id, safe="")
        return f"{GRAPH_MESSAGE_ENDPOINT}/{encoded_id}"

    @staticmethod
    def _headers(token: GraphAccessToken) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token.access_token}",
            "Accept": "application/json",
            "Prefer": GRAPH_IMMUTABLE_ID_PREFER,
        }

    @staticmethod
    def _raise_read_error(response: httpx.Response) -> None:
        if 300 <= response.status_code < 400:
            raise GraphWriteRedirectRejectedError(
                "Microsoft Graph preflight read redirect was rejected"
            )
        if response.status_code in (401, 403):
            raise GraphAuthorizationError(_safe_graph_error(response))
        if response.status_code == 429:
            raw_retry_after = response.headers.get("Retry-After")
            retry_after = (
                int(raw_retry_after) if raw_retry_after and raw_retry_after.isdigit() else None
            )
            raise GraphThrottledError(retry_after)
        if response.status_code >= 400:
            raise GraphServiceError(_safe_graph_error(response))
        if response.status_code != 200:
            raise GraphServiceError(
                f"Microsoft Graph returned unexpected preflight status HTTP {response.status_code}"
            )

    def get_category_snapshot(
        self,
        message_id: str,
        token: GraphAccessToken,
    ) -> GraphMessageCategorySnapshot:
        """Read only id, categories, and changeKey from one immutable message."""

        validated = GraphCategoryWriteRequest(message_id=message_id, categories=())
        url = f"{self._message_url(validated.message_id)}?$select=id,categories,changeKey"
        try:
            response = self._http_client.get(
                url,
                headers=self._headers(token),
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise GraphServiceError(
                f"Microsoft Graph preflight read failed: {type(error).__name__}"
            ) from error
        self._raise_read_error(response)

        try:
            payload = response.json()
        except ValueError as error:
            raise GraphServiceError(
                "Microsoft Graph preflight read returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise GraphServiceError("Microsoft Graph preflight response must be a JSON object")

        response_message_id = payload.get("id")
        categories = payload.get("categories")
        change_key = payload.get("changeKey")
        if (
            not isinstance(response_message_id, str)
            or not isinstance(categories, list)
            or not all(isinstance(category, str) for category in categories)
            or not isinstance(change_key, str)
        ):
            raise GraphServiceError(
                "Microsoft Graph preflight response is missing id, categories, or changeKey"
            )
        try:
            snapshot = GraphMessageCategorySnapshot(
                message_id=response_message_id,
                categories=tuple(categories),
                change_key=change_key,
            )
        except ValueError as error:
            raise GraphServiceError(
                "Microsoft Graph preflight response failed validation"
            ) from error
        if snapshot.message_id != validated.message_id:
            raise GraphServiceError("Microsoft Graph preflight response message ID does not match")
        return snapshot

    def set_categories(
        self,
        request: GraphCategoryWriteRequest,
        token: GraphAccessToken,
    ) -> GraphCategoryWriteResult:
        """Send one non-redirecting category-only PATCH and verify the response."""

        url = self._message_url(request.message_id)
        headers = self._headers(token)
        headers["Content-Type"] = "application/json"
        try:
            response = self._http_client.patch(
                url,
                headers=headers,
                json={"categories": list(request.categories)},
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise GraphWriteOutcomeUnknownError(
                f"Microsoft Graph category write outcome is unknown: {type(error).__name__}"
            ) from error

        if 300 <= response.status_code < 400:
            raise GraphWriteRedirectRejectedError(
                "Microsoft Graph category write redirect was rejected"
            )
        if response.status_code in (401, 403):
            raise GraphAuthorizationError(_safe_graph_error(response))
        if response.status_code == 429:
            raw_retry_after = response.headers.get("Retry-After")
            retry_after = (
                int(raw_retry_after) if raw_retry_after and raw_retry_after.isdigit() else None
            )
            raise GraphThrottledError(retry_after)
        if response.status_code in (409, 412):
            raise GraphWriteConflictError(_safe_graph_error(response))
        if response.status_code >= 400:
            raise GraphServiceError(_safe_graph_error(response))
        if response.status_code != 200:
            raise GraphWriteOutcomeUnknownError(
                f"Microsoft Graph returned unexpected write status HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as error:
            raise GraphWriteOutcomeUnknownError(
                "Microsoft Graph category write returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise GraphWriteOutcomeUnknownError(
                "Microsoft Graph category write response must be a JSON object"
            )

        message_id = payload.get("id")
        categories = payload.get("categories")
        change_key = payload.get("changeKey")
        if (
            not isinstance(message_id, str)
            or not isinstance(categories, list)
            or not all(isinstance(category, str) for category in categories)
            or not isinstance(change_key, str)
        ):
            raise GraphWriteOutcomeUnknownError(
                "Microsoft Graph category write response is missing id, categories, or changeKey"
            )

        try:
            result = GraphCategoryWriteResult(
                message_id=message_id,
                categories=tuple(categories),
                change_key=change_key,
            )
        except ValueError as error:
            raise GraphWriteOutcomeUnknownError(
                "Microsoft Graph category write response failed validation"
            ) from error

        expected_categories = {category.casefold() for category in request.categories}
        actual_categories = {category.casefold() for category in result.categories}
        if result.message_id != request.message_id or actual_categories != expected_categories:
            raise GraphWriteOutcomeUnknownError(
                "Microsoft Graph accepted the write but the returned message could not be verified"
            )
        return result
