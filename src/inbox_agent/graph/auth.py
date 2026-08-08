"""MSAL device-code authentication with encrypted delegated token caching."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import msal  # type: ignore[import-untyped]
from msal_extensions import (  # type: ignore[import-untyped]
    PersistedTokenCache,
    build_encrypted_persistence,
)

from inbox_agent.graph.config import GraphSettings, GraphWriteSettings


class PublicClientProtocol(Protocol):
    """Small MSAL surface used by the connector and deterministic tests."""

    def get_accounts(self, username: str | None = None) -> list[dict[str, Any]]: ...

    def acquire_token_silent(
        self,
        scopes: list[str],
        account: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any] | None: ...

    def initiate_device_flow(
        self,
        scopes: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def acquire_token_by_device_flow(
        self,
        flow: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True, repr=False)
class GraphAccessToken:
    """In-memory bearer token that deliberately hides its value from repr."""

    access_token: str
    username: str | None = None

    def __repr__(self) -> str:
        return f"GraphAccessToken(username={self.username!r}, access_token=<redacted>)"


class GraphAuthenticationError(Exception):
    """Base class for safe, non-secret delegated authentication failures."""


class GraphLoginRequiredError(GraphAuthenticationError):
    """Raised when no cached account can satisfy a silent token request."""


class GraphTokenCacheError(GraphAuthenticationError):
    """Raised when encrypted token persistence cannot be initialized."""


class GraphDeviceFlowError(GraphAuthenticationError):
    """Raised when Microsoft cannot initialize device-code authentication."""


class GraphTokenAcquisitionError(GraphAuthenticationError):
    """Raised when an interactive or silent MSAL result contains an error."""

    def __init__(
        self,
        error_code: str,
        description: str,
        correlation_id: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.description = description
        self.correlation_id = correlation_id
        suffix = f" (correlation_id={correlation_id})" if correlation_id else ""
        super().__init__(f"Microsoft authentication failed: {error_code}: {description}{suffix}")


def _safe_text(value: object, fallback: str, maximum_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return value.replace("\r", " ").replace("\n", " ").strip()[:maximum_length]


def _token_from_result(result: Mapping[str, object]) -> GraphAccessToken:
    access_token = result.get("access_token")
    if isinstance(access_token, str) and access_token:
        account = result.get("id_token_claims")
        username: str | None = None
        if isinstance(account, Mapping):
            preferred = account.get("preferred_username")
            if isinstance(preferred, str) and preferred:
                username = preferred
        return GraphAccessToken(access_token=access_token, username=username)

    error_code = _safe_text(result.get("error"), "token_not_returned", 100)
    description = _safe_text(
        result.get("error_description"),
        "Microsoft identity platform returned no access token",
    )
    correlation_id_value = result.get("correlation_id")
    correlation_id = (
        _safe_text(correlation_id_value, "", 100) if isinstance(correlation_id_value, str) else None
    )
    raise GraphTokenAcquisitionError(error_code, description, correlation_id or None)


class GraphTokenProvider:
    """Acquire delegated Graph tokens silently or through device code."""

    def __init__(
        self,
        app: PublicClientProtocol,
        scopes: Sequence[str],
        *,
        login_command: str = "outlook login",
    ) -> None:
        self._app = app
        self._scopes = tuple(scopes)
        self._login_command = login_command

    @classmethod
    def from_settings(
        cls,
        settings: GraphSettings | GraphWriteSettings,
        project_root: Path,
    ) -> GraphTokenProvider:
        """Build MSAL with an OS-encrypted, locked token cache under data/private."""

        cache_path = project_root / settings.token_cache_path
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            persistence = build_encrypted_persistence(str(cache_path))
            cache = PersistedTokenCache(persistence)
            app = msal.PublicClientApplication(
                client_id=settings.client_id,
                authority=settings.authority,
                token_cache=cache,
            )
        except Exception as error:  # noqa: BLE001 - platform persistence boundary
            raise GraphTokenCacheError(
                f"Unable to initialize encrypted MSAL token cache: {type(error).__name__}"
            ) from error
        login_command = (
            "outlook write-login" if isinstance(settings, GraphWriteSettings) else "outlook login"
        )
        return cls(
            cast(PublicClientProtocol, app),
            settings.scopes,
            login_command=login_command,
        )

    def acquire_silent(self) -> GraphAccessToken:
        """Return a cached/refreshed token without presenting an interactive prompt."""

        accounts = self._app.get_accounts()
        if not accounts:
            raise GraphLoginRequiredError(
                f"No cached Outlook account; run {self._login_command} first"
            )
        result = self._app.acquire_token_silent(list(self._scopes), account=accounts[0])
        if result is None:
            raise GraphLoginRequiredError(
                "Cached Outlook account cannot provide the configured Graph scope; "
                f"run {self._login_command} again"
            )
        return _token_from_result(result)

    def login(self, display_message: Callable[[str], None]) -> GraphAccessToken:
        """Use cache when possible, otherwise complete delegated device-code login."""

        try:
            return self.acquire_silent()
        except GraphLoginRequiredError:
            pass

        flow = self._app.initiate_device_flow(scopes=list(self._scopes))
        if "user_code" not in flow:
            raise GraphDeviceFlowError(
                _safe_text(
                    flow.get("error_description"),
                    "Microsoft identity platform did not return a device code",
                )
            )
        display_message(
            _safe_text(
                flow.get("message"),
                "Open https://microsoft.com/devicelogin and enter the displayed code.",
                2_000,
            )
        )
        result = self._app.acquire_token_by_device_flow(flow)
        return _token_from_result(result)
