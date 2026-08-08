"""Tests for strict read-only Microsoft Graph configuration."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from inbox_agent.graph import (
    GraphAccountAudience,
    GraphSettings,
    GraphSettingsNotFoundError,
    GraphSettingsValidationError,
    GraphSettingsYAMLError,
    GraphWriteDisabledError,
    GraphWriteSettings,
    load_graph_settings,
    load_graph_write_settings,
)

CLIENT_ID = "12345678-1234-4234-8234-123456789abc"


def test_graph_settings_default_to_personal_read_only_outlook() -> None:
    settings = GraphSettings(client_id=CLIENT_ID)

    assert settings.account_audience is GraphAccountAudience.PERSONAL
    assert settings.authority == "https://login.microsoftonline.com/consumers"
    assert settings.scopes == ("Mail.Read",)
    assert settings.mail_folder == "inbox"
    assert settings.dataset_path == Path("data/private/outlook_inbox.json")


def test_graph_write_settings_are_disabled_and_cache_isolated_by_default() -> None:
    settings = GraphWriteSettings(client_id=CLIENT_ID)

    assert settings.scopes == ("Mail.ReadWrite",)
    assert settings.write_enabled is False
    assert settings.token_cache_path == Path("data/private/msal_write_token_cache.bin")
    assert settings.token_cache_path != GraphSettings(client_id=CLIENT_ID).token_cache_path
    with pytest.raises(GraphWriteDisabledError, match="write_enabled: true"):
        settings.require_enabled()


def test_graph_write_settings_require_explicit_opt_in() -> None:
    settings = GraphWriteSettings(client_id=CLIENT_ID, write_enabled=True)

    settings.require_enabled()


@pytest.mark.parametrize(
    "scopes",
    [
        ("Mail.ReadWrite",),
        ("Mail.Send",),
        ("Mail.Read", "User.Read"),
        (),
    ],
)
def test_graph_settings_reject_nonminimal_permissions(scopes: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match="delegated Mail.Read"):
        GraphSettings(client_id=CLIENT_ID, scopes=scopes)


@pytest.mark.parametrize(
    "scopes",
    [
        ("Mail.Read",),
        ("Mail.Send",),
        ("Mail.ReadWrite", "MailboxSettings.ReadWrite"),
        (),
    ],
)
def test_graph_write_settings_reject_unnecessary_permissions(
    scopes: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError, match="delegated Mail.ReadWrite"):
        GraphWriteSettings(client_id=CLIENT_ID, scopes=scopes)


def test_graph_write_settings_reject_shared_or_custom_token_cache() -> None:
    with pytest.raises(ValidationError, match="write token cache"):
        GraphWriteSettings(
            client_id=CLIENT_ID,
            token_cache_path=Path("data/private/msal_token_cache.bin"),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("token_cache_path", "token.bin"),
        ("sync_state_path", "../private/state.json"),
        ("dataset_path", "C:/mail/outlook.json"),
        ("dataset_path", "data/private"),
    ],
)
def test_graph_settings_keep_sensitive_files_under_private_data(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match="private data paths"):
        GraphSettings.model_validate({"client_id": CLIENT_ID, field: value})


def test_graph_settings_support_explicit_future_account_audiences() -> None:
    organizational = GraphSettings(
        client_id=CLIENT_ID,
        account_audience=GraphAccountAudience.ORGANIZATIONAL,
    )
    any_account = GraphSettings(
        client_id=CLIENT_ID,
        account_audience=GraphAccountAudience.ANY,
    )

    assert organizational.authority.endswith("/organizations")
    assert any_account.authority.endswith("/common")


def test_load_graph_settings_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "graph.yaml"
    path.write_text(
        f"client_id: {CLIENT_ID}\naccount_audience: consumers\nscopes:\n  - Mail.Read\n",
        encoding="utf-8",
    )

    settings = load_graph_settings(path)

    assert settings.client_id == CLIENT_ID
    assert settings.initial_sync_days == 30


def test_load_graph_write_settings_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "graph_write.yaml"
    path.write_text(
        f"client_id: {CLIENT_ID}\nscopes: [Mail.ReadWrite]\nwrite_enabled: true\n",
        encoding="utf-8",
    )

    settings = load_graph_write_settings(path)

    assert settings.write_enabled is True
    assert settings.token_cache_path.name == "msal_write_token_cache.bin"


def test_graph_settings_report_missing_yaml_and_schema(tmp_path: Path) -> None:
    with pytest.raises(GraphSettingsNotFoundError):
        load_graph_settings(tmp_path / "missing.yaml")

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("client_id: [", encoding="utf-8")
    with pytest.raises(GraphSettingsYAMLError):
        load_graph_settings(malformed)

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("client_id: not-a-uuid\nscopes: [Mail.Send]", encoding="utf-8")
    with pytest.raises(GraphSettingsValidationError) as captured:
        load_graph_settings(invalid)
    assert captured.value.validation_error.error_count() == 2
