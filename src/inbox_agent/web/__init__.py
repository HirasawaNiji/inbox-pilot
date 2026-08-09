"""Local-only FastAPI surface for InboxPilot."""

from inbox_agent.web.app import create_app
from inbox_agent.web.config import WebSettings

__all__ = ["WebSettings", "create_app"]
