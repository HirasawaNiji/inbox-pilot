"""Shared FastAPI dependencies for JSON and HTML surfaces."""

from __future__ import annotations

from typing import cast

from fastapi import Request

from inbox_agent.storage import Database, current_revision, head_revision
from inbox_agent.web.actions import WebActionService, WebOperationError
from inbox_agent.web.query import WebQueryService


def require_query_service(request: Request) -> WebQueryService:
    """Return a query service only when the private database is current."""

    database = cast(Database | None, getattr(request.app.state, "database", None))
    if database is None:
        raise WebOperationError(503, "DATABASE_UNAVAILABLE", "The local database is unavailable")
    revision = current_revision(database.engine)
    expected = head_revision(database.path)
    if revision is None or revision != expected:
        raise WebOperationError(
            503,
            "DATABASE_UPGRADE_REQUIRED",
            "The local database must be upgraded before it can be queried",
        )
    return WebQueryService(database)


def require_action_service(request: Request) -> WebActionService:
    """Return the existing action safety service stored by the app lifespan."""

    return cast(WebActionService, request.app.state.action_service)
