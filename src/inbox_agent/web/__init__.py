"""Local-only FastAPI surface for InboxPilot."""

from inbox_agent.web.app import create_app
from inbox_agent.web.config import WebSettings
from inbox_agent.web.shutdown import WebShutdownController

__all__ = ["WebSettings", "WebShutdownController", "create_app"]
