"""HTTP-neutral application service around the Stage 3 action safety boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Never

import httpx
from pydantic import ValidationError

from inbox_agent.actions import (
    ActionActor,
    ActionAuditLog,
    ActionAuditStorageError,
    ActionExecutionAuditError,
    ActionExecutionGuardError,
    ActionExecutionPersistenceError,
    ActionGraphExecutionReport,
    ActionQueue,
    ActionQueueConflictError,
    ActionQueueNotFoundError,
    ActionQueueRepository,
    ActionQueueStorageError,
    ActionReconciliationReport,
    ApprovedActionGraphExecutor,
    ControlledRollbackExecutor,
    DryRunReport,
    MailboxAction,
    MailboxActionStatus,
    RollbackDryRunReport,
    RollbackExecutionReport,
    RollbackPlanError,
    RollbackReconciliationReport,
    UncertainActionReconciler,
    UncertainRollbackReconciler,
    audit_event_for_rollback_dry_run,
    audit_events_for_action,
    audit_events_for_dry_run,
    build_dry_run,
    build_rollback_dry_run,
)
from inbox_agent.graph import (
    GraphAccessToken,
    GraphAuthenticationError,
    GraphCategoryWriteClient,
    GraphRequestError,
    GraphSettingsError,
    GraphTokenProvider,
    GraphWriteDisabledError,
    GraphWriteSettings,
    load_graph_write_settings,
)
from inbox_agent.web.config import WebSettings
from inbox_agent.web.schemas import ActionPage


@dataclass(frozen=True, slots=True)
class WebOperationError(RuntimeError):
    status_code: int
    code: str
    safe_message: str

    def __str__(self) -> str:
        return self.safe_message


class WebActionService:
    """Reuse the existing queue, lock, audit and Graph executors without bypasses."""

    def __init__(self, settings: WebSettings) -> None:
        self.settings = settings

    def _repository(self) -> ActionQueueRepository:
        return ActionQueueRepository(self.settings.action_queue_path)

    def _audit(self) -> ActionAuditLog:
        return ActionAuditLog(self.settings.audit_log_path)

    @staticmethod
    def _raise_safe(error: Exception) -> Never:
        if isinstance(error, ActionQueueNotFoundError):
            raise WebOperationError(404, "ACTION_NOT_FOUND", "The action does not exist") from error
        if isinstance(error, (ActionQueueConflictError, ActionExecutionGuardError)):
            raise WebOperationError(
                409,
                "ACTION_CONFLICT",
                "The action state changed or does not permit this operation",
            ) from error
        if isinstance(error, GraphWriteDisabledError):
            raise WebOperationError(
                403,
                "OUTLOOK_WRITE_DISABLED",
                "Outlook write authorization is disabled",
            ) from error
        if isinstance(error, GraphAuthenticationError):
            raise WebOperationError(
                503,
                "OUTLOOK_LOGIN_REQUIRED",
                "Outlook write authorization requires an interactive login",
            ) from error
        if isinstance(error, GraphRequestError):
            raise WebOperationError(
                502,
                "GRAPH_OPERATION_FAILED",
                "Microsoft Graph did not complete the requested operation",
            ) from error
        if isinstance(error, (GraphSettingsError, ValidationError, RollbackPlanError)):
            raise WebOperationError(
                422,
                "INVALID_OPERATION",
                "The operation is not valid",
            ) from error
        if isinstance(
            error,
            (
                ActionQueueStorageError,
                ActionAuditStorageError,
                ActionExecutionPersistenceError,
                ActionExecutionAuditError,
            ),
        ):
            raise WebOperationError(
                503,
                "LOCAL_STATE_UNAVAILABLE",
                "The protected local action state is unavailable",
            ) from error
        raise error

    def list_actions(self, status: MailboxActionStatus | None = None) -> ActionPage:
        try:
            actions = self._repository().load().actions
        except Exception as error:
            self._raise_safe(error)
        if status is not None:
            actions = tuple(action for action in actions if action.status is status)
        return ActionPage(items=actions, total=len(actions))

    def get_action(self, action_id: str) -> MailboxAction:
        try:
            action = self._repository().load().find(action_id)
        except Exception as error:
            self._raise_safe(error)
        if action is None:
            raise WebOperationError(404, "ACTION_NOT_FOUND", "The action does not exist")
        return action

    def _review(
        self,
        action_id: str,
        status: MailboxActionStatus,
        note: str | None,
    ) -> MailboxAction:
        try:
            action = self._repository().transition(
                action_id,
                status,
                actor=ActionActor.USER,
                note=note,
            )
            self._audit().append_unique(audit_events_for_action(action))
            return action
        except Exception as error:
            self._raise_safe(error)

    def approve(self, action_id: str, note: str | None) -> MailboxAction:
        return self._review(action_id, MailboxActionStatus.APPROVED, note)

    def reject(self, action_id: str, reason: str | None) -> MailboxAction:
        return self._review(action_id, MailboxActionStatus.REJECTED, reason)

    def preview(self, action_id: str) -> DryRunReport:
        try:
            queue = self._repository().load()
            action = queue.find(action_id)
            if action is None:
                raise ActionQueueNotFoundError(f"Action does not exist: {action_id}")
            selected = ActionQueue(updated_at=queue.updated_at, actions=(action,))
            report = build_dry_run(selected, self.settings.action_queue_path)
            events = (
                *audit_events_for_action(action),
                *audit_events_for_dry_run((action,), report),
            )
            self._audit().append_unique(events)
            return report
        except Exception as error:
            self._raise_safe(error)

    def _write_context(
        self,
    ) -> tuple[GraphWriteSettings, GraphAccessToken, ActionQueueRepository, ActionAuditLog]:
        graph_settings = load_graph_write_settings(self.settings.graph_write_config_path)
        graph_settings.require_enabled()
        provider = GraphTokenProvider.from_settings(graph_settings, self.settings.project_root)
        token = provider.acquire_silent()
        repository = self._repository()
        audit = self._audit()
        return graph_settings, token, repository, audit

    def execute(
        self,
        action_id: str,
        idempotency_key: str,
        confirm_action_id: str,
    ) -> ActionGraphExecutionReport:
        if confirm_action_id != action_id:
            raise WebOperationError(
                409,
                "CONFIRMATION_MISMATCH",
                "The confirmation must exactly match the action ID",
            )
        try:
            graph_settings, token, repository, audit = self._write_context()
            with httpx.Client() as client:
                return ApprovedActionGraphExecutor(
                    repository,
                    GraphCategoryWriteClient(graph_settings, client),
                    audit,
                ).execute(action_id, idempotency_key, token)
        except Exception as error:
            self._raise_safe(error)

    def reconcile(self, action_id: str, idempotency_key: str) -> ActionReconciliationReport:
        try:
            graph_settings, token, repository, audit = self._write_context()
            with httpx.Client() as client:
                return UncertainActionReconciler(
                    repository,
                    GraphCategoryWriteClient(graph_settings, client),
                    audit,
                ).reconcile(action_id, idempotency_key, token)
        except Exception as error:
            self._raise_safe(error)

    def rollback_preview(self, action_id: str, reason: str) -> RollbackDryRunReport:
        try:
            queue = self._repository().load()
            report = build_rollback_dry_run(
                queue,
                action_id,
                self.settings.action_queue_path,
                reason=reason,
            )
            action = queue.find(action_id)
            if action is None:
                raise ActionQueueNotFoundError(f"Action does not exist: {action_id}")
            self._audit().append_unique(
                (*audit_events_for_action(action), audit_event_for_rollback_dry_run(action, report))
            )
            return report
        except Exception as error:
            self._raise_safe(error)

    def rollback_execute(
        self,
        action_id: str,
        rollback_idempotency_key: str,
        reason: str,
        confirm_action_id: str,
    ) -> RollbackExecutionReport:
        if confirm_action_id != action_id:
            raise WebOperationError(
                409,
                "CONFIRMATION_MISMATCH",
                "The confirmation must exactly match the action ID",
            )
        try:
            graph_settings, token, repository, audit = self._write_context()
            with httpx.Client() as client:
                return ControlledRollbackExecutor(
                    repository,
                    GraphCategoryWriteClient(graph_settings, client),
                    audit,
                ).execute(action_id, rollback_idempotency_key, reason, token)
        except Exception as error:
            self._raise_safe(error)

    def rollback_reconcile(
        self,
        action_id: str,
        rollback_idempotency_key: str,
    ) -> RollbackReconciliationReport:
        try:
            graph_settings, token, repository, audit = self._write_context()
            with httpx.Client() as client:
                return UncertainRollbackReconciler(
                    repository,
                    GraphCategoryWriteClient(graph_settings, client),
                    audit,
                ).reconcile(action_id, rollback_idempotency_key, token)
        except Exception as error:
            self._raise_safe(error)
