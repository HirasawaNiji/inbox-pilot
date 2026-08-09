"""Server-rendered local console built on the existing application services."""

from __future__ import annotations

import secrets
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from starlette.background import BackgroundTask
from starlette.datastructures import FormData
from starlette.templating import Jinja2Templates

from inbox_agent.actions import MailboxAction, MailboxActionStatus
from inbox_agent.models import Priority
from inbox_agent.service import ServiceConfigurationError, inspect_service, load_service_settings
from inbox_agent.web.actions import WebActionService, WebOperationError
from inbox_agent.web.agent_manager import PROVIDER_MODELS, WebAgentError, WebAgentManager
from inbox_agent.web.config import WebSettings
from inbox_agent.web.dependencies import require_action_service, require_query_service
from inbox_agent.web.query import MessageFilters, WebQueryNotFoundError, WebQueryService
from inbox_agent.web.schemas import (
    ExecuteRequest,
    RejectRequest,
    ReviewRequest,
    RollbackExecuteRequest,
    RollbackPreviewRequest,
)
from inbox_agent.web.shutdown import WebShutdownController

TEMPLATE_ROOT = Path(__file__).with_name("templates")
STATIC_ROOT = Path(__file__).with_name("static")
CSRF_COOKIE = "inboxpilot_csrf"

templates = Jinja2Templates(directory=str(TEMPLATE_ROOT))


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


templates.env.filters["local_datetime"] = _format_datetime


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    values: dict[str, Any] = {
        "request": request,
        "csrf_token": request.app.state.console_csrf_token,
        "current_path": request.url.path,
        "console_url": f"{str(request.base_url).rstrip('/')}/console",
    }
    if context:
        values.update(context)
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context=values,
        status_code=status_code,
    )
    response.set_cookie(
        CSRF_COOKIE,
        request.app.state.console_csrf_token,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/console",
    )
    return response


def render_console_error(
    request: Request,
    status_code: int,
    code: str,
    message: str,
) -> HTMLResponse:
    """Render a privacy-safe error page for browser routes."""

    return _render(
        request,
        "error.html",
        {"error_code": code, "error_message": message},
        status_code=status_code,
    )


async def _verified_form(request: Request) -> FormData:
    form = await request.form()
    supplied = form.get("_csrf")
    cookie = request.cookies.get(CSRF_COOKIE)
    expected = request.app.state.console_csrf_token
    if not isinstance(supplied, str) or cookie is None:
        raise WebOperationError(403, "CSRF_REJECTED", "The form confirmation expired")
    if not secrets.compare_digest(supplied, expected) or not secrets.compare_digest(
        cookie, expected
    ):
        raise WebOperationError(403, "CSRF_REJECTED", "The form confirmation expired")
    return form


def _form_text(
    form: FormData,
    field: str,
    *,
    required: bool = False,
    max_length: int = 1_000,
) -> str | None:
    value = form.get(field)
    if value is None:
        if required:
            raise WebOperationError(422, "INVALID_FORM", "A required form field is missing")
        return None
    if not isinstance(value, str):
        raise WebOperationError(422, "INVALID_FORM", "The form contains an invalid value")
    normalized = value.strip()
    if required and not normalized:
        raise WebOperationError(422, "INVALID_FORM", "A required form field is missing")
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise WebOperationError(422, "INVALID_FORM", "A form value is too long")
    return normalized


def _validated(model_type: type[Any], **values: object) -> Any:
    try:
        return model_type.model_validate(values)
    except ValidationError as error:
        raise WebOperationError(422, "INVALID_FORM", "The submitted form is invalid") from error


def _message_filters(
    priority: Priority | None,
    category: str | None,
    review: str | None,
    offset: int,
) -> tuple[MessageFilters, bool | None]:
    requires_review = {"yes": True, "no": False}.get(review or "")
    filters = MessageFilters(
        priority=priority,
        category=category.strip() if category else None,
        requires_review=requires_review,
        limit=25,
        offset=offset,
    )
    return filters, requires_review


def _page_url(path: str, filters: MessageFilters, offset: int) -> str:
    values: dict[str, str | int] = {"offset": offset}
    if filters.priority is not None:
        values["priority"] = filters.priority.value
    if filters.category:
        values["category"] = filters.category
    if filters.requires_review is not None:
        values["review"] = "yes" if filters.requires_review else "no"
    return f"{path}?{urlencode(values)}"


def _message_context(service: WebQueryService, database_id: int) -> dict[str, Any]:
    detail = service.get_message(database_id)
    rule_priority = (
        detail.rule_evaluation.suggested_priority if detail.rule_evaluation is not None else None
    )
    llm_priority = (
        detail.llm_analysis.analysis.priority if detail.llm_analysis is not None else None
    )
    priority_conflict = (
        rule_priority is not None and llm_priority is not None and rule_priority != llm_priority
    )
    review_reasons: list[str] = []
    if detail.rule_evaluation is not None and detail.rule_evaluation.requires_review:
        review_reasons.append("YAML 规则要求人工复核")
    if detail.llm_analysis is not None and detail.llm_analysis.analysis.requires_review:
        review_reasons.append("LLM 结果要求人工复核")
    if priority_conflict:
        review_reasons.append("规则与 LLM 的优先级判断存在冲突")
    if detail.triage is not None and detail.triage.requires_review and not review_reasons:
        review_reasons.append("最终融合策略要求人工确认")
    return {
        "detail": detail,
        "priority_conflict": priority_conflict,
        "review_reasons": tuple(review_reasons),
    }


def _scheduler_status(settings: WebSettings) -> dict[str, Any]:
    try:
        service_settings = load_service_settings(settings.service_config_path)
        report = inspect_service(
            service_settings,
            config_path=settings.service_config_path,
            project_root=settings.project_root,
        )
        return {"available": True, "report": report}
    except ServiceConfigurationError:
        return {"available": False, "report": None}


def _agent_manager(request: Request) -> WebAgentManager:
    return cast(WebAgentManager, request.app.state.agent_manager)


def _agent_operation(operation: Any) -> Any:
    try:
        return operation()
    except WebAgentError as error:
        raise WebOperationError(409, error.code, error.safe_message) from error


def _action_context(action: MailboxAction, *, notice: str | None = None) -> dict[str, Any]:
    return {
        "action": action,
        "notice": notice,
        "can_review": action.status is MailboxActionStatus.PENDING_REVIEW,
        "can_preview": action.status is MailboxActionStatus.APPROVED,
        "can_reconcile": action.status is MailboxActionStatus.OUTCOME_UNKNOWN,
        "can_rollback": action.status is MailboxActionStatus.SUCCEEDED,
        "can_rollback_reconcile": (action.status is MailboxActionStatus.ROLLBACK_OUTCOME_UNKNOWN),
    }


def _action_response(
    request: Request,
    action: MailboxAction,
    notice: str,
) -> Response:
    if _is_htmx(request):
        return _render(
            request,
            "partials/action_panel.html",
            _action_context(action, notice=notice),
        )
    return RedirectResponse(f"/console/actions/{action.action_id}", status_code=303)


def build_console_router(settings: WebSettings) -> APIRouter:
    """Build the local console routes around shared query and action services."""

    router = APIRouter(prefix="/console", include_in_schema=False)

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        query: Annotated[WebQueryService, Depends(require_query_service)],
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> HTMLResponse:
        recent = query.list_messages(MessageFilters(limit=6))
        priority_counts = {
            priority.value: query.list_messages(MessageFilters(priority=priority, limit=1)).total
            for priority in Priority
        }
        review_count = query.list_messages(MessageFilters(requires_review=True, limit=1)).total
        action_page = actions.list_actions()
        action_counts = Counter(action.status.value for action in action_page.items)
        try:
            workflow = query.latest_workflow()
        except WebQueryNotFoundError:
            workflow = None
        return _render(
            request,
            "dashboard.html",
            {
                "recent": recent,
                "priority_counts": priority_counts,
                "review_count": review_count,
                "action_counts": action_counts,
                "workflow": workflow,
                "scheduler": _scheduler_status(settings),
                "agent": _agent_manager(request).status(),
            },
        )

    def render_inbox(
        request: Request,
        query: WebQueryService,
        priority: Priority | None,
        category: str | None,
        review: str | None,
        offset: int,
        *,
        partial: bool,
    ) -> HTMLResponse:
        filters, requires_review = _message_filters(priority, category, review, offset)
        page = query.list_messages(filters)
        context = {
            "page": page,
            "filters": filters,
            "review_filter": review,
            "requires_review": requires_review,
            "previous_url": (
                _page_url("/console/inbox", filters, max(0, offset - page.limit))
                if offset > 0
                else None
            ),
            "next_url": (
                _page_url("/console/inbox", filters, offset + page.limit)
                if offset + page.limit < page.total
                else None
            ),
        }
        return _render(
            request,
            "partials/message_table.html" if partial else "inbox.html",
            context,
        )

    @router.get("/inbox", response_class=HTMLResponse)
    def inbox(
        request: Request,
        query: Annotated[WebQueryService, Depends(require_query_service)],
        priority: Annotated[Priority | None, Query()] = None,
        category: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        review: Annotated[str | None, Query(pattern=r"^(yes|no)$")] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> HTMLResponse:
        return render_inbox(request, query, priority, category, review, offset, partial=False)

    @router.get("/inbox/table", response_class=HTMLResponse)
    def inbox_table(
        request: Request,
        query: Annotated[WebQueryService, Depends(require_query_service)],
        priority: Annotated[Priority | None, Query()] = None,
        category: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        review: Annotated[str | None, Query(pattern=r"^(yes|no)$")] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> HTMLResponse:
        return render_inbox(request, query, priority, category, review, offset, partial=True)

    @router.get("/messages/{database_id}", response_class=HTMLResponse)
    def message_detail(
        database_id: int,
        request: Request,
        query: Annotated[WebQueryService, Depends(require_query_service)],
    ) -> HTMLResponse:
        return _render(request, "message_detail.html", _message_context(query, database_id))

    @router.get("/reviews", response_class=HTMLResponse)
    def reviews(
        request: Request,
        query: Annotated[WebQueryService, Depends(require_query_service)],
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> HTMLResponse:
        messages = query.list_messages(MessageFilters(requires_review=True, limit=50))
        pending = actions.list_actions(MailboxActionStatus.PENDING_REVIEW)
        return _render(request, "reviews.html", {"messages": messages, "actions": pending})

    @router.get("/actions", response_class=HTMLResponse)
    def action_list(
        request: Request,
        actions: Annotated[WebActionService, Depends(require_action_service)],
        status: Annotated[MailboxActionStatus | None, Query()] = None,
    ) -> HTMLResponse:
        return _render(
            request,
            "actions.html",
            {"page": actions.list_actions(status), "selected_status": status},
        )

    @router.get("/actions/{action_id}", response_class=HTMLResponse)
    def action_detail(
        action_id: str,
        request: Request,
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> HTMLResponse:
        return _render(
            request,
            "action_detail.html",
            _action_context(actions.get_action(action_id)),
        )

    @router.post("/actions/{action_id}/approve", response_class=HTMLResponse)
    async def approve(
        action_id: str,
        request: Request,
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> Response:
        form = await _verified_form(request)
        payload = _validated(ReviewRequest, note=_form_text(form, "note"))
        action = actions.approve(action_id, payload.note)
        return _action_response(request, action, "动作已批准，可以生成写回预览。")

    @router.post("/actions/{action_id}/reject", response_class=HTMLResponse)
    async def reject(
        action_id: str,
        request: Request,
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> Response:
        form = await _verified_form(request)
        payload = _validated(RejectRequest, reason=_form_text(form, "reason"))
        action = actions.reject(action_id, payload.reason)
        return _action_response(request, action, "动作已拒绝，不会写回 Outlook。")

    @router.get("/actions/{action_id}/execute", response_class=HTMLResponse)
    def execute_confirmation(
        action_id: str,
        request: Request,
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> HTMLResponse:
        action = actions.get_action(action_id)
        preview = actions.preview(action_id)
        return _render(
            request,
            "execute_confirmation.html",
            {"action": action, "preview": preview, "plan": preview.plans[0]},
        )

    @router.post("/actions/{action_id}/execute", response_class=HTMLResponse)
    async def execute(
        action_id: str,
        request: Request,
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> HTMLResponse:
        form = await _verified_form(request)
        payload = _validated(
            ExecuteRequest,
            confirm_action_id=_form_text(form, "confirm_action_id", required=True, max_length=128),
            idempotency_key=_form_text(form, "idempotency_key", required=True, max_length=64),
        )
        report = actions.execute(action_id, payload.idempotency_key, payload.confirm_action_id)
        action = actions.get_action(action_id)
        return _render(
            request,
            "operation_result.html",
            {"title": "写回结果", "report": report, "action": action},
        )

    @router.post("/actions/{action_id}/reconcile", response_class=HTMLResponse)
    async def reconcile(
        action_id: str,
        request: Request,
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> HTMLResponse:
        await _verified_form(request)
        action = actions.get_action(action_id)
        if action.idempotency_key is None:
            raise WebOperationError(409, "ACTION_CONFLICT", "The action cannot be reconciled")
        report = actions.reconcile(action_id, action.idempotency_key)
        return _render(
            request,
            "operation_result.html",
            {
                "title": "正向写回对账结果",
                "report": report,
                "action": actions.get_action(action_id),
            },
        )

    @router.post("/actions/{action_id}/rollback/preview", response_class=HTMLResponse)
    async def rollback_preview(
        action_id: str,
        request: Request,
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> HTMLResponse:
        form = await _verified_form(request)
        payload = _validated(
            RollbackPreviewRequest,
            reason=_form_text(form, "reason", required=True),
        )
        preview = actions.rollback_preview(action_id, payload.reason)
        return _render(
            request,
            "rollback_confirmation.html",
            {"action": actions.get_action(action_id), "preview": preview, "plan": preview.plan},
        )

    @router.post("/actions/{action_id}/rollback/execute", response_class=HTMLResponse)
    async def rollback_execute(
        action_id: str,
        request: Request,
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> HTMLResponse:
        form = await _verified_form(request)
        payload = _validated(
            RollbackExecuteRequest,
            reason=_form_text(form, "reason", required=True),
            confirm_action_id=_form_text(form, "confirm_action_id", required=True, max_length=128),
            rollback_idempotency_key=_form_text(
                form, "rollback_idempotency_key", required=True, max_length=64
            ),
        )
        report = actions.rollback_execute(
            action_id,
            payload.rollback_idempotency_key,
            payload.reason,
            payload.confirm_action_id,
        )
        return _render(
            request,
            "operation_result.html",
            {
                "title": "回滚结果",
                "report": report,
                "action": actions.get_action(action_id),
            },
        )

    @router.post("/actions/{action_id}/rollback/reconcile", response_class=HTMLResponse)
    async def rollback_reconcile(
        action_id: str,
        request: Request,
        actions: Annotated[WebActionService, Depends(require_action_service)],
    ) -> HTMLResponse:
        await _verified_form(request)
        action = actions.get_action(action_id)
        if action.rollback_snapshot is None:
            raise WebOperationError(409, "ACTION_CONFLICT", "The rollback cannot be reconciled")
        report = actions.rollback_reconcile(
            action_id, action.rollback_snapshot.rollback_idempotency_key
        )
        return _render(
            request,
            "operation_result.html",
            {
                "title": "回滚对账结果",
                "report": report,
                "action": actions.get_action(action_id),
            },
        )

    @router.get("/operations", response_class=HTMLResponse)
    def operations(
        request: Request,
        query: Annotated[WebQueryService, Depends(require_query_service)],
        notice: Annotated[str | None, Query(pattern=r"^(sync-started|sync-stopping)$")] = None,
    ) -> HTMLResponse:
        try:
            workflow = query.latest_workflow()
        except WebQueryNotFoundError:
            workflow = None
        return _render(
            request,
            "operations.html",
            {
                "workflow": workflow,
                "scheduler": _scheduler_status(settings),
                "agent": _agent_manager(request).status(),
                "notice": notice,
            },
        )

    @router.post("/operations/sync/start", response_class=HTMLResponse)
    async def sync_start(request: Request) -> Response:
        await _verified_form(request)
        _agent_operation(_agent_manager(request).start_sync)
        return RedirectResponse("/console/operations?notice=sync-started", status_code=303)

    @router.post("/operations/sync/stop", response_class=HTMLResponse)
    async def sync_stop(request: Request) -> Response:
        await _verified_form(request)
        _agent_operation(_agent_manager(request).stop_sync)
        return RedirectResponse("/console/operations?notice=sync-stopping", status_code=303)

    @router.get("/settings", response_class=HTMLResponse)
    def agent_settings(request: Request) -> HTMLResponse:
        return _render(
            request,
            "settings.html",
            {
                "agent": _agent_manager(request).status(),
                "provider_models": {
                    provider.value: models for provider, models in PROVIDER_MODELS.items()
                },
            },
        )

    @router.post("/settings/llm", response_class=HTMLResponse)
    async def llm_settings(request: Request) -> Response:
        form = await _verified_form(request)
        enabled = _form_text(form, "enabled", max_length=10) == "true"
        manager = _agent_manager(request)
        _agent_operation(
            lambda: manager.configure_llm(
                enabled=enabled,
                provider=_form_text(form, "provider", max_length=32),
                model=_form_text(form, "model", max_length=200),
                api_key=_form_text(form, "api_key", max_length=10_000),
            )
        )
        return RedirectResponse("/console/settings?saved=true", status_code=303)

    @router.get("/system/background", response_class=HTMLResponse)
    def background_mode(request: Request) -> HTMLResponse:
        return _render(request, "background_mode.html")

    @router.get("/system/shutdown", response_class=HTMLResponse)
    def shutdown_confirmation(request: Request) -> HTMLResponse:
        controller = cast(WebShutdownController, request.app.state.shutdown_controller)
        return _render(
            request,
            "shutdown_confirmation.html",
            {"shutdown_available": controller.available},
        )

    @router.post("/system/shutdown", response_class=HTMLResponse)
    async def shutdown(request: Request) -> HTMLResponse:
        form = await _verified_form(request)
        confirmation = _form_text(
            form,
            "confirmation",
            required=True,
            max_length=16,
        )
        if confirmation != "EXIT":
            raise WebOperationError(
                409,
                "SHUTDOWN_CONFIRMATION_MISMATCH",
                "Type EXIT exactly to confirm a full shutdown",
            )
        controller = cast(WebShutdownController, request.app.state.shutdown_controller)
        if not controller.available:
            raise WebOperationError(
                409,
                "WEB_SHUTDOWN_UNAVAILABLE",
                "Restart with 'inbox-agent web start' to enable safe Web shutdown",
            )
        response = _render(request, "shutdown_complete.html")
        response.background = BackgroundTask(controller.request_shutdown)
        return response

    return router
