"""Tests for delegated MSAL device-code and silent authentication."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from inbox_agent.graph import (
    GraphDeviceFlowError,
    GraphLoginRequiredError,
    GraphTokenAcquisitionError,
    GraphTokenProvider,
)


class FakePublicClient:
    def __init__(
        self,
        *,
        accounts: list[dict[str, Any]] | None = None,
        silent_result: dict[str, Any] | None = None,
        device_flow: dict[str, Any] | None = None,
        device_result: dict[str, Any] | None = None,
    ) -> None:
        self.accounts = accounts or []
        self.silent_result = silent_result
        self.device_flow = device_flow or {
            "user_code": "ABCD-EFGH",
            "message": "Open the browser and enter ABCD-EFGH",
        }
        self.device_result = device_result or {"access_token": "device-token"}
        self.silent_calls = 0
        self.device_calls = 0

    def get_accounts(self, username: str | None = None) -> list[dict[str, Any]]:
        return list(self.accounts)

    def acquire_token_silent(
        self,
        scopes: list[str],
        account: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        self.silent_calls += 1
        assert isinstance(scopes, list)
        assert tuple(scopes) == ("Mail.Read",)
        return self.silent_result

    def initiate_device_flow(
        self,
        scopes: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert isinstance(scopes, list)
        assert tuple(scopes or ()) == ("Mail.Read",)
        return dict(self.device_flow)

    def acquire_token_by_device_flow(
        self,
        flow: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.device_calls += 1
        return dict(self.device_result)


def test_silent_authentication_returns_redacted_token_object() -> None:
    app = FakePublicClient(
        accounts=[{"username": "student@outlook.com"}],
        silent_result={
            "access_token": "secret-access-token",
            "id_token_claims": {"preferred_username": "student@outlook.com"},
        },
    )
    provider = GraphTokenProvider(app, ("Mail.Read",))

    token = provider.acquire_silent()

    assert token.access_token == "secret-access-token"
    assert token.username == "student@outlook.com"
    assert "secret-access-token" not in repr(token)
    assert "<redacted>" in repr(token)


def test_silent_authentication_requires_cached_account_and_token() -> None:
    with pytest.raises(GraphLoginRequiredError, match="run outlook login"):
        GraphTokenProvider(FakePublicClient(), ("Mail.Read",)).acquire_silent()

    app = FakePublicClient(accounts=[{"username": "student@outlook.com"}])
    with pytest.raises(GraphLoginRequiredError, match="run outlook login again"):
        GraphTokenProvider(app, ("Mail.Read",)).acquire_silent()


def test_login_uses_cached_token_without_device_prompt() -> None:
    app = FakePublicClient(
        accounts=[{"username": "student@outlook.com"}],
        silent_result={"access_token": "cached-token"},
    )
    messages: list[str] = []

    token = GraphTokenProvider(app, ("Mail.Read",)).login(messages.append)

    assert token.access_token == "cached-token"
    assert messages == []
    assert app.device_calls == 0


def test_login_falls_back_to_device_code_and_displays_safe_message() -> None:
    app = FakePublicClient(device_result={"access_token": "interactive-token"})
    messages: list[str] = []

    token = GraphTokenProvider(app, ("Mail.Read",)).login(messages.append)

    assert token.access_token == "interactive-token"
    assert messages == ["Open the browser and enter ABCD-EFGH"]
    assert app.device_calls == 1


def test_login_reports_device_flow_initialization_failure() -> None:
    app = FakePublicClient(
        device_flow={"error": "invalid_client", "error_description": "Public flow disabled"}
    )

    with pytest.raises(GraphDeviceFlowError, match="Public flow disabled"):
        GraphTokenProvider(app, ("Mail.Read",)).login(lambda _: None)


def test_token_error_is_bounded_and_does_not_include_access_token() -> None:
    app = FakePublicClient(
        device_result={
            "error": "authorization_declined",
            "error_description": "The user declined consent.\nTry again.",
            "correlation_id": "correlation-123",
        }
    )

    with pytest.raises(GraphTokenAcquisitionError) as captured:
        GraphTokenProvider(app, ("Mail.Read",)).login(lambda _: None)

    assert captured.value.error_code == "authorization_declined"
    assert "The user declined consent. Try again." in str(captured.value)
    assert "correlation-123" in str(captured.value)
