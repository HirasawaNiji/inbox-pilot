"""FastAPI application factory for the loopback-only InboxPilot API."""

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from inbox_agent.actions import MailboxAction, MailboxActionStatus
from inbox_agent.models import Priority
from inbox_agent.service import ServiceConfigurationError, inspect_service, load_service_settings
from inbox_agent.storage import Database, current_revision, head_revision
from inbox_agent.web.actions import WebActionService, WebOperationError
from inbox_agent.web.agent_manager import WebAgentManager
from inbox_agent.web.config import WebSettings
from inbox_agent.web.console import STATIC_ROOT, build_console_router, render_console_error
from inbox_agent.web.dependencies import require_action_service, require_query_service
from inbox_agent.web.query import MessageFilters, WebQueryNotFoundError, WebQueryService
from inbox_agent.web.schemas import (
    ActionExecuteResponse,
    ActionPage,
    ActionPreviewResponse,
    ActionReconcileResponse,
    ErrorBody,
    ErrorResponse,
    ExecuteRequest,
    HealthResponse,
    MessageDetail,
    MessagePage,
    ReconcileRequest,
    RejectRequest,
    ReviewRequest,
    RollbackExecuteRequest,
    RollbackExecuteResponse,
    RollbackPreviewRequest,
    RollbackPreviewResponse,
    RollbackReconcileRequest,
    RollbackReconcileResponse,
    WorkflowRunResponse,
)
from inbox_agent.web.shutdown import WebShutdownController

LOGGER = logging.getLogger(__name__)
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def _error(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=getattr(request.state, "request_id", None),
        )
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def create_app(
    settings: WebSettings | None = None,
    *,
    shutdown_controller: WebShutdownController | None = None,
    agent_manager: WebAgentManager | None = None,
) -> FastAPI:
    """Build one local API instance without migrating or creating private state."""

    web_settings = (settings or WebSettings.from_environment()).resolved()
    managed_agent = agent_manager or WebAgentManager(web_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = (
            Database(web_settings.database_path) if web_settings.database_path.is_file() else None
        )
        app.state.database = database
        app.state.settings = web_settings
        app.state.action_service = WebActionService(web_settings)
        app.state.console_csrf_token = secrets.token_urlsafe(32)
        app.state.shutdown_controller = shutdown_controller or WebShutdownController()
        app.state.agent_manager = managed_agent
        try:
            yield
        finally:
            managed_agent.shutdown()
            if database is not None:
                database.dispose()

    app = FastAPI(
        title="InboxPilot Local API",
        version="0.1.0",
        description=(
            "Loopback-only API over InboxPilot's existing storage, review, audit, "
            "write-preflight, reconciliation and rollback layers."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"],
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: object) -> object:
        request.state.request_id = uuid4().hex
        response = await call_next(request)  # type: ignore[operator]
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/console"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
                "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            )
        return response

    @app.exception_handler(WebOperationError)
    async def operation_error(request: Request, error: WebOperationError) -> Response:
        if request.url.path.startswith("/console"):
            return render_console_error(request, error.status_code, error.code, error.safe_message)
        return _error(request, error.status_code, error.code, error.safe_message)

    @app.exception_handler(WebQueryNotFoundError)
    async def not_found(request: Request, error: WebQueryNotFoundError) -> Response:
        del error
        if request.url.path.startswith("/console"):
            return render_console_error(
                request, 404, "NOT_FOUND", "The requested resource does not exist"
            )
        return _error(request, 404, "NOT_FOUND", "The requested resource does not exist")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> Response:
        del error
        if request.url.path.startswith("/console"):
            return render_console_error(
                request, 422, "INVALID_REQUEST", "Request validation failed"
            )
        return _error(request, 422, "INVALID_REQUEST", "Request validation failed")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception) -> Response:
        LOGGER.exception("Unhandled Web API error request_id=%s", request.state.request_id)
        if request.url.path.startswith("/console"):
            return render_console_error(
                request, 500, "INTERNAL_ERROR", "The request could not be completed"
            )
        return _error(request, 500, "INTERNAL_ERROR", "The request could not be completed")

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"name": "InboxPilot Local API", "docs": "/docs", "health": "/api/v1/health"}

    @app.get("/api/v1/health", response_model=HealthResponse)
    def health(request: Request) -> HealthResponse:
        database_path = web_settings.database_path
        expected = head_revision(database_path) or "unknown"
        database = cast(Database | None, getattr(request.app.state, "database", None))
        if database is None:
            return HealthResponse(
                status="degraded",
                database_exists=False,
                expected_revision=expected,
                database_ready=False,
            )
        revision = current_revision(database.engine)
        if revision is None:
            return HealthResponse(
                status="degraded",
                database_exists=True,
                expected_revision=expected,
                database_ready=False,
            )
        return WebQueryService(database).health(revision=revision, expected_revision=expected)

    @app.get("/api/v1/messages", response_model=MessagePage, responses=ERROR_RESPONSES)
    def messages(
        service: Annotated[WebQueryService, Depends(require_query_service)],
        priority: Annotated[Priority | None, Query()] = None,
        category: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        requires_review: Annotated[bool | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> MessagePage:
        return service.list_messages(
            MessageFilters(
                priority=priority,
                category=category,
                requires_review=requires_review,
                limit=limit,
                offset=offset,
            )
        )

    @app.get(
        "/api/v1/messages/{database_id}", response_model=MessageDetail, responses=ERROR_RESPONSES
    )
    def message_detail(
        database_id: int,
        service: Annotated[WebQueryService, Depends(require_query_service)],
    ) -> MessageDetail:
        return service.get_message(database_id)

    @app.get("/api/v1/reviews", response_model=MessagePage, responses=ERROR_RESPONSES)
    def reviews(
        service: Annotated[WebQueryService, Depends(require_query_service)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> MessagePage:
        return service.list_messages(
            MessageFilters(requires_review=True, limit=limit, offset=offset)
        )

    @app.get("/api/v1/actions", response_model=ActionPage, responses=ERROR_RESPONSES)
    def actions(
        service: Annotated[WebActionService, Depends(require_action_service)],
        status: Annotated[MailboxActionStatus | None, Query()] = None,
    ) -> ActionPage:
        return service.list_actions(status)

    @app.get("/api/v1/actions/{action_id}", response_model=MailboxAction, responses=ERROR_RESPONSES)
    def action_detail(
        action_id: str,
        service: Annotated[WebActionService, Depends(require_action_service)],
    ) -> MailboxAction:
        return service.get_action(action_id)

    @app.post(
        "/api/v1/actions/{action_id}/approve",
        response_model=MailboxAction,
        responses=ERROR_RESPONSES,
    )
    def approve(
        action_id: str,
        payload: ReviewRequest,
        service: Annotated[WebActionService, Depends(require_action_service)],
    ) -> MailboxAction:
        return service.approve(action_id, payload.note)

    @app.post(
        "/api/v1/actions/{action_id}/reject",
        response_model=MailboxAction,
        responses=ERROR_RESPONSES,
    )
    def reject(
        action_id: str,
        payload: RejectRequest,
        service: Annotated[WebActionService, Depends(require_action_service)],
    ) -> MailboxAction:
        return service.reject(action_id, payload.reason)

    @app.post(
        "/api/v1/actions/{action_id}/preview",
        response_model=ActionPreviewResponse,
        responses=ERROR_RESPONSES,
    )
    def preview(
        action_id: str,
        service: Annotated[WebActionService, Depends(require_action_service)],
    ) -> object:
        return service.preview(action_id)

    @app.post(
        "/api/v1/actions/{action_id}/execute",
        response_model=ActionExecuteResponse,
        responses=ERROR_RESPONSES,
    )
    def execute(
        action_id: str,
        payload: ExecuteRequest,
        service: Annotated[WebActionService, Depends(require_action_service)],
    ) -> object:
        return service.execute(action_id, payload.idempotency_key, payload.confirm_action_id)

    @app.post(
        "/api/v1/actions/{action_id}/reconcile",
        response_model=ActionReconcileResponse,
        responses=ERROR_RESPONSES,
    )
    def reconcile(
        action_id: str,
        payload: ReconcileRequest,
        service: Annotated[WebActionService, Depends(require_action_service)],
    ) -> object:
        return service.reconcile(action_id, payload.idempotency_key)

    @app.post(
        "/api/v1/actions/{action_id}/rollback/preview",
        response_model=RollbackPreviewResponse,
        responses=ERROR_RESPONSES,
    )
    def rollback_preview(
        action_id: str,
        payload: RollbackPreviewRequest,
        service: Annotated[WebActionService, Depends(require_action_service)],
    ) -> object:
        return service.rollback_preview(action_id, payload.reason)

    @app.post(
        "/api/v1/actions/{action_id}/rollback/execute",
        response_model=RollbackExecuteResponse,
        responses=ERROR_RESPONSES,
    )
    def rollback_execute(
        action_id: str,
        payload: RollbackExecuteRequest,
        service: Annotated[WebActionService, Depends(require_action_service)],
    ) -> object:
        return service.rollback_execute(
            action_id,
            payload.rollback_idempotency_key,
            payload.reason,
            payload.confirm_action_id,
        )

    @app.post(
        "/api/v1/actions/{action_id}/rollback/reconcile",
        response_model=RollbackReconcileResponse,
        responses=ERROR_RESPONSES,
    )
    def rollback_reconcile(
        action_id: str,
        payload: RollbackReconcileRequest,
        service: Annotated[WebActionService, Depends(require_action_service)],
    ) -> object:
        return service.rollback_reconcile(action_id, payload.rollback_idempotency_key)

    @app.get(
        "/api/v1/workflows/runs/latest",
        response_model=WorkflowRunResponse,
        responses=ERROR_RESPONSES,
    )
    def latest_workflow(
        service: Annotated[WebQueryService, Depends(require_query_service)],
    ) -> WorkflowRunResponse:
        return service.latest_workflow()

    @app.get(
        "/api/v1/workflows/runs/{run_id}",
        response_model=WorkflowRunResponse,
        responses=ERROR_RESPONSES,
    )
    def workflow(
        run_id: str,
        service: Annotated[WebQueryService, Depends(require_query_service)],
    ) -> WorkflowRunResponse:
        return service.workflow(run_id)

    @app.get("/api/v1/service/status", responses=ERROR_RESPONSES)
    def service_status() -> object:
        try:
            service_settings = load_service_settings(web_settings.service_config_path)
            return inspect_service(
                service_settings,
                config_path=web_settings.service_config_path,
                project_root=web_settings.project_root,
            )
        except ServiceConfigurationError as error:
            raise WebOperationError(
                503,
                "SERVICE_CONFIGURATION_UNAVAILABLE",
                "The local scheduler configuration is unavailable",
            ) from error

    app.include_router(build_console_router(web_settings))
    return app
