"""Strict local configuration for delegated Microsoft Graph access."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path, PurePosixPath

import yaml
from pydantic import Field, ValidationError, field_validator
from yaml import YAMLError

from inbox_agent.models import FrozenModel


class GraphAccountAudience(StrEnum):
    """Microsoft identity audiences supported by the local public client."""

    PERSONAL = "consumers"
    ORGANIZATIONAL = "organizations"
    ANY = "common"


class GraphSettings(FrozenModel):
    """Validated settings for read-only personal Outlook synchronization."""

    client_id: str = Field(
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    account_audience: GraphAccountAudience = GraphAccountAudience.PERSONAL
    scopes: tuple[str, ...] = ("Mail.Read",)
    mail_folder: str = Field(default="inbox", pattern=r"^inbox$")
    initial_sync_days: int = Field(default=30, ge=1, le=365)
    page_size: int = Field(default=50, ge=1, le=100)
    request_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    token_cache_path: Path = Path("data/private/msal_token_cache.bin")
    sync_state_path: Path = Path("data/private/graph_sync_state.json")
    dataset_path: Path = Path("data/private/outlook_inbox.json")

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Keep the connector limited to delegated read-only mailbox access."""

        if value != ("Mail.Read",):
            raise ValueError("scopes must contain only delegated Mail.Read")
        return value

    @field_validator("token_cache_path", "sync_state_path", "dataset_path")
    @classmethod
    def validate_private_path(cls, value: Path) -> Path:
        """Prevent credentials or real mail from being written outside data/private."""

        normalized = PurePosixPath(value.as_posix())
        if value.is_absolute() or ".." in normalized.parts:
            raise ValueError("private data paths must be relative and must not contain '..'")
        if normalized.parts[:2] != ("data", "private") or len(normalized.parts) < 3:
            raise ValueError("private data paths must be located under data/private")
        return Path(normalized.as_posix())

    @property
    def authority(self) -> str:
        """Return the MSAL authority for the configured account audience."""

        return f"https://login.microsoftonline.com/{self.account_audience.value}"


class GraphWriteDisabledError(Exception):
    """Raised when the local write-capability safety switch is disabled."""


class GraphWriteSettings(FrozenModel):
    """Validated, isolated settings for delegated Outlook write authorization."""

    client_id: str = Field(
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    account_audience: GraphAccountAudience = GraphAccountAudience.PERSONAL
    scopes: tuple[str, ...] = ("Mail.ReadWrite",)
    write_enabled: bool = False
    request_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    token_cache_path: Path = Path("data/private/msal_write_token_cache.bin")

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Allow only the delegated permission needed for message categories."""

        if value != ("Mail.ReadWrite",):
            raise ValueError("scopes must contain only delegated Mail.ReadWrite")
        return value

    @field_validator("token_cache_path")
    @classmethod
    def validate_write_cache_path(cls, value: Path) -> Path:
        """Keep the write token in a fixed cache isolated from read-only tokens."""

        expected = Path("data/private/msal_write_token_cache.bin")
        if value != expected:
            raise ValueError("write token cache must be data/private/msal_write_token_cache.bin")
        return value

    @property
    def authority(self) -> str:
        """Return the MSAL authority for the configured account audience."""

        return f"https://login.microsoftonline.com/{self.account_audience.value}"

    def require_enabled(self) -> None:
        """Require explicit local opt-in before write-capable authorization."""

        if not self.write_enabled:
            raise GraphWriteDisabledError(
                "Outlook write authorization is disabled; set write_enabled: true "
                "in the private write configuration after reviewing the permission"
            )


class GraphSettingsError(Exception):
    """Base class for Graph configuration loading failures."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class GraphSettingsNotFoundError(GraphSettingsError):
    """Raised when the Graph configuration file is missing."""


class GraphSettingsReadError(GraphSettingsError):
    """Raised when the Graph configuration file cannot be read."""


class GraphSettingsYAMLError(GraphSettingsError):
    """Raised when the Graph configuration file is invalid YAML."""


class GraphSettingsValidationError(GraphSettingsError):
    """Raised when Graph YAML violates the strict configuration schema."""

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(path, "Graph settings do not match the InboxPilot schema")


def _load_graph_payload(path: str | Path) -> tuple[Path, object]:
    """Read one UTF-8 Graph YAML payload with safe, shared error handling."""

    settings_path = Path(path)
    try:
        raw_content = settings_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise GraphSettingsNotFoundError(
            settings_path,
            "Graph settings file does not exist",
        ) from error
    except OSError as error:
        raise GraphSettingsReadError(
            settings_path,
            "Unable to read Graph settings file",
        ) from error

    try:
        payload = yaml.safe_load(raw_content)
    except YAMLError as error:
        raise GraphSettingsYAMLError(
            settings_path,
            "Graph settings contain invalid YAML",
        ) from error

    return settings_path, payload


def load_graph_settings(path: str | Path) -> GraphSettings:
    """Read and validate one UTF-8 read-only Graph configuration file."""

    settings_path, payload = _load_graph_payload(path)
    try:
        return GraphSettings.model_validate(payload)
    except ValidationError as error:
        raise GraphSettingsValidationError(settings_path, error) from error


def load_graph_write_settings(path: str | Path) -> GraphWriteSettings:
    """Read and validate one UTF-8 write-authorization Graph configuration file."""

    settings_path, payload = _load_graph_payload(path)
    try:
        return GraphWriteSettings.model_validate(payload)
    except ValidationError as error:
        raise GraphSettingsValidationError(settings_path, error) from error
