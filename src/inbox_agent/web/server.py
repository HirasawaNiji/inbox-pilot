"""Managed loopback-only Uvicorn launcher for the local console."""

from __future__ import annotations

import uvicorn

from inbox_agent.web.app import create_app
from inbox_agent.web.config import WebSettings
from inbox_agent.web.shutdown import WebShutdownController


def run_web_server(
    settings: WebSettings | None = None,
    *,
    port: int = 8765,
    log_level: str = "info",
) -> None:
    """Run Uvicorn while exposing only its graceful stop signal to the app."""

    controller = WebShutdownController()
    application = create_app(settings, shutdown_controller=controller)
    config = uvicorn.Config(
        app=application,
        host="127.0.0.1",
        port=port,
        log_level=log_level,
    )
    server = uvicorn.Server(config)

    def stop_server() -> None:
        server.should_exit = True

    controller.bind(stop_server)
    server.run()
