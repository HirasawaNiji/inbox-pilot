"""Tests for explicit background and managed Web shutdown behavior."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from inbox_agent.web import WebSettings, WebShutdownController, create_app


def settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        project_root=tmp_path,
        database_path=tmp_path / "missing.sqlite3",
        action_queue_path=tmp_path / "actions.json",
        audit_log_path=tmp_path / "audit.jsonl",
        graph_write_config_path=tmp_path / "graph_write.local.yaml",
        service_config_path=tmp_path / "service.local.yaml",
    )


def test_background_page_keeps_process_available(tmp_path: Path) -> None:
    controller = WebShutdownController()
    stopped: list[bool] = []
    controller.bind(lambda: stopped.append(True))

    with TestClient(create_app(settings(tmp_path), shutdown_controller=controller)) as client:
        response = client.get("/console/system/background")
        health = client.get("/api/v1/health")

    assert response.status_code == 200
    assert "可以安全关闭这个网页" in response.text
    assert health.status_code == 200
    assert not controller.requested
    assert stopped == []


def test_shutdown_requires_csrf_and_exact_confirmation(tmp_path: Path) -> None:
    controller = WebShutdownController()
    stopped: list[bool] = []
    controller.bind(lambda: stopped.append(True))

    with TestClient(create_app(settings(tmp_path), shutdown_controller=controller)) as client:
        page = client.get("/console/system/shutdown")
        csrf = client.cookies.get("inboxpilot_csrf")
        missing_csrf = client.post(
            "/console/system/shutdown",
            data={"confirmation": "EXIT"},
        )
        mismatch = client.post(
            "/console/system/shutdown",
            data={"_csrf": csrf, "confirmation": "exit"},
        )

    assert page.status_code == 200
    assert csrf is not None
    assert missing_csrf.status_code == 403
    assert mismatch.status_code == 409
    assert "SHUTDOWN_CONFIRMATION_MISMATCH" in mismatch.text
    assert not controller.requested
    assert stopped == []


def test_managed_shutdown_runs_after_response(tmp_path: Path) -> None:
    controller = WebShutdownController()
    stopped: list[bool] = []
    controller.bind(lambda: stopped.append(True))

    with TestClient(create_app(settings(tmp_path), shutdown_controller=controller)) as client:
        page = client.get("/console/system/shutdown")
        csrf = client.cookies.get("inboxpilot_csrf")
        response = client.post(
            "/console/system/shutdown",
            data={"_csrf": csrf, "confirmation": "EXIT"},
        )

    assert page.status_code == 200
    assert response.status_code == 200
    assert "Web 进程已收到退出请求" in response.text
    assert controller.requested
    assert stopped == [True]


def test_raw_uvicorn_instance_refuses_unsafe_process_shutdown(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        page = client.get("/console/system/shutdown")
        csrf = client.cookies.get("inboxpilot_csrf")
        response = client.post(
            "/console/system/shutdown",
            data={"_csrf": csrf, "confirmation": "EXIT"},
        )

    assert page.status_code == 200
    assert "当前实例由原始 Uvicorn 命令启动" in page.text
    assert response.status_code == 409
    assert "WEB_SHUTDOWN_UNAVAILABLE" in response.text


def test_shutdown_controller_is_one_shot() -> None:
    controller = WebShutdownController()
    calls: list[bool] = []

    assert not controller.request_shutdown()
    controller.bind(lambda: calls.append(True))
    assert controller.request_shutdown()
    assert controller.request_shutdown()
    assert calls == [True]
