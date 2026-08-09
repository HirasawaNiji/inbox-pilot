"""Tests for Web-managed synchronization and memory-only LLM settings."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from inbox_agent.actions.locking import ActionFileLock
from inbox_agent.web import WebSettings, create_app
from inbox_agent.web.agent_manager import WebAgentError, WebAgentManager


def web_settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        project_root=tmp_path,
        database_path=tmp_path / "private" / "web.sqlite3",
        action_queue_path=tmp_path / "private" / "actions.json",
        audit_log_path=tmp_path / "private" / "audit.jsonl",
        graph_write_config_path=tmp_path / "private" / "graph_write.local.yaml",
        service_config_path=tmp_path / "service.local.yaml",
    )


def write_service_config(tmp_path: Path) -> None:
    (tmp_path / "service.local.yaml").write_text(
        """
schema_version: "1.0"
service_name: inbox-pilot-test
interval_minutes: 5
max_backoff_minutes: 10
run_immediately: false
lock_path: private/service.lock
workflow:
  dataset_path: messages.json
  database_path: private/service.sqlite3
  action_queue_path: private/service-actions.json
  audit_log_path: private/service-audit.jsonl
  policy_path: rules.yaml
  llm_config_path: null
  sync_outlook: false
""".strip(),
        encoding="utf-8",
    )


def wait_until(predicate: object, *, timeout: float = 2.0) -> None:
    callback = predicate
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(callback) and callback():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_llm_is_disabled_by_default_and_key_never_returns_or_persists(tmp_path: Path) -> None:
    settings = web_settings(tmp_path)
    manager = WebAgentManager(settings)
    secret = "sk-memory-only-secret-value"

    with TestClient(create_app(settings, agent_manager=manager)) as client:
        page = client.get("/console/settings")
        csrf = client.cookies.get("inboxpilot_csrf")
        response = client.post(
            "/console/settings/llm",
            data={
                "_csrf": csrf,
                "enabled": "true",
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "api_key": secret,
            },
            follow_redirects=False,
        )
        configured = client.get("/console/settings")

        assert page.status_code == 200
        assert "默认关闭" in page.text
        assert response.status_code == 303
        assert configured.status_code == 200
        assert "gpt-5.6-luna" in configured.text
        assert "gpt-5.6-sol" in configured.text
        assert "gpt-5.6-terra" in configured.text
        assert "deepseek-v4-flash" in configured.text
        assert "deepseek-v4-pro" in configured.text
        assert secret not in response.text
        assert secret not in configured.text
        assert manager.status().llm_enabled

    assert not manager.status().llm_enabled
    secret_bytes = secret.encode()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret_bytes not in path.read_bytes()


def test_sync_controls_require_csrf_and_manage_one_scheduler_thread(tmp_path: Path) -> None:
    write_service_config(tmp_path)
    settings = web_settings(tmp_path)
    manager = WebAgentManager(settings)

    with TestClient(create_app(settings, agent_manager=manager)) as client:
        settings_page = client.get("/console/settings")
        csrf = client.cookies.get("inboxpilot_csrf")
        rejected = client.post("/console/operations/sync/start")
        started = client.post(
            "/console/operations/sync/start",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        wait_until(lambda: manager.status().sync_owned)
        stopped = client.post(
            "/console/operations/sync/stop",
            data={"_csrf": csrf},
            follow_redirects=False,
        )

        assert settings_page.status_code == 200
        assert rejected.status_code == 403
        assert started.status_code == 303
        assert stopped.status_code == 303

    assert not manager.status().sync_active


def test_external_scheduler_lock_prevents_duplicate_web_start(tmp_path: Path) -> None:
    write_service_config(tmp_path)
    settings = web_settings(tmp_path)
    manager = WebAgentManager(settings)
    lock_path = tmp_path / "private" / "service.lock"

    with ActionFileLock(lock_path, timeout_seconds=0):
        status = manager.status()
        with pytest.raises(WebAgentError) as captured:
            manager.start_sync()

    assert status.sync_external
    assert captured.value.code == "EXTERNAL_SYNC_ACTIVE"


def test_llm_settings_cannot_change_during_managed_sync(tmp_path: Path) -> None:
    write_service_config(tmp_path)
    manager = WebAgentManager(web_settings(tmp_path))
    manager.start_sync()
    wait_until(lambda: manager.status().sync_owned)

    try:
        with pytest.raises(WebAgentError) as captured:
            manager.configure_llm(
                enabled=True,
                provider="deepseek",
                model="deepseek-v4-flash",
                api_key="secret",
            )
    finally:
        manager.stop_sync()
        manager.shutdown()

    assert captured.value.code == "SYNC_ACTIVE"


def test_model_must_match_selected_provider(tmp_path: Path) -> None:
    manager = WebAgentManager(web_settings(tmp_path))

    with pytest.raises(WebAgentError) as captured:
        manager.configure_llm(
            enabled=True,
            provider="openai",
            model="deepseek-v4-pro",
            api_key="secret",
        )

    assert captured.value.code == "INVALID_LLM_SETTINGS"
