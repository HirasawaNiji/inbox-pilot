"""Tests for conflict-safe execution of approved Graph category actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from inbox_agent.actions import (
    ActionActor,
    ActionAuditEvent,
    ActionAuditEventType,
    ActionAuditLog,
    ActionAuditStorageError,
    ActionExecutionAuditError,
    ActionExecutionGuardError,
    ActionExecutionPersistenceError,
    ActionGraphExecutionOutcome,
    ActionQueueRepository,
    ActionQueueStorageError,
    ActionReconciliationOutcome,
    ApprovedActionGraphExecutor,
    AuditAppendReport,
    AuditGraphOperation,
    AuditGraphOutcome,
    MailboxAction,
    MailboxActionStatus,
    UncertainActionReconciler,
    build_review_actions,
)
from inbox_agent.graph import (
    GraphAccessToken,
    GraphCategoryWriteRequest,
    GraphCategoryWriteResult,
    GraphMessageCategorySnapshot,
    GraphServiceError,
    GraphWriteOutcomeUnknownError,
)
from inbox_agent.loader import load_dataset
from inbox_agent.pipeline import OfflinePipeline

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rules.yaml"
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
CREATED_AT = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)


def make_action(
    *,
    categories: tuple[str, ...] = ("School",),
    change_key: str | None = "approved-change-key",
) -> MailboxAction:
    dataset = load_dataset(DATASET_PATH)
    message = dataset.messages[0].model_copy(
        update={"categories": categories, "change_key": change_key}
    )
    dataset = dataset.model_copy(update={"messages": (message,)})
    analysis = OfflinePipeline.from_yaml(POLICY_PATH).analyze_dataset(
        dataset,
        evaluated_at=CREATED_AT,
    )
    return build_review_actions(dataset, analysis)[0]


def approved_repository(
    tmp_path: Path,
    action: MailboxAction,
) -> tuple[ActionQueueRepository, list[datetime]]:
    current_time = [CREATED_AT + timedelta(minutes=1)]
    repository = ActionQueueRepository(
        tmp_path / "action_queue.json",
        clock=lambda: current_time[0],
    )
    repository.enqueue((action,))
    repository.transition(
        action.action_id,
        MailboxActionStatus.APPROVED,
        actor=ActionActor.USER,
    )
    current_time[0] += timedelta(minutes=1)
    return repository, current_time


class FakeGraphClient:
    def __init__(
        self,
        snapshot: GraphMessageCategorySnapshot,
        *,
        read_error: Exception | None = None,
        write_error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.read_error = read_error
        self.write_error = write_error
        self.read_message_ids: list[str] = []
        self.write_requests: list[GraphCategoryWriteRequest] = []

    def get_category_snapshot(
        self,
        message_id: str,
        token: GraphAccessToken,
    ) -> GraphMessageCategorySnapshot:
        self.read_message_ids.append(message_id)
        if self.read_error is not None:
            raise self.read_error
        return self.snapshot

    def set_categories(
        self,
        request: GraphCategoryWriteRequest,
        token: GraphAccessToken,
    ) -> GraphCategoryWriteResult:
        self.write_requests.append(request)
        if self.write_error is not None:
            raise self.write_error
        return GraphCategoryWriteResult(
            message_id=request.message_id,
            categories=request.categories,
            change_key="written-change-key",
        )


def live_snapshot(
    action: MailboxAction,
    *,
    categories: tuple[str, ...] | None = None,
    change_key: str = "approved-change-key",
) -> GraphMessageCategorySnapshot:
    return GraphMessageCategorySnapshot(
        message_id=action.message_id,
        categories=categories if categories is not None else action.current_snapshot.categories,
        change_key=change_key,
    )


def execute(
    repository: ActionQueueRepository,
    action: MailboxAction,
    graph: FakeGraphClient,
):
    assert action.idempotency_key is not None
    audit_log = ActionAuditLog(repository.path.with_name("actions.jsonl"))
    return ApprovedActionGraphExecutor(repository, graph, audit_log).execute(
        action.action_id,
        action.idempotency_key,
        GraphAccessToken("secret-token"),
    )


def test_executor_reads_live_state_then_preserves_unmanaged_categories_and_writes(
    tmp_path: Path,
) -> None:
    action = make_action(categories=("School", "Important"))
    repository, _ = approved_repository(tmp_path, action)
    graph = FakeGraphClient(live_snapshot(action))

    report = execute(repository, action, graph)

    assert report.outcome is ActionGraphExecutionOutcome.SUCCEEDED
    assert report.final_status is MailboxActionStatus.SUCCEEDED
    assert report.graph_read_request_count == 1
    assert report.graph_write_request_count == 1
    assert graph.read_message_ids == [action.message_id]
    assert len(graph.write_requests) == 1
    assert graph.write_requests[0].categories == (
        "School",
        "Important",
        *action.write_plan.managed_categories,
    )
    audit_events = ActionAuditLog(repository.path.with_name("actions.jsonl")).load()
    assert [event.action_status for event in audit_events] == [
        MailboxActionStatus.PENDING_REVIEW,
        MailboxActionStatus.APPROVED,
        MailboxActionStatus.EXECUTING,
        MailboxActionStatus.WRITE_IN_FLIGHT,
        MailboxActionStatus.SUCCEEDED,
        MailboxActionStatus.SUCCEEDED,
    ]
    operation_event = audit_events[-1]
    assert operation_event.event_type is ActionAuditEventType.GRAPH_OPERATION_RECORDED
    assert operation_event.graph_operation is not None
    assert operation_event.graph_operation.operation is AuditGraphOperation.EXECUTE
    assert operation_event.graph_operation.outcome is AuditGraphOutcome.SUCCEEDED
    assert operation_event.graph_operation.graph_read_request_count == 1
    assert operation_event.graph_operation.graph_write_request_count == 1
    assert action.message_id not in repository.path.with_name("actions.jsonl").read_text("utf-8")


@pytest.mark.parametrize(
    "live_categories,live_change_key,reason",
    [
        (("School",), "different-change-key", "changeKey changed"),
        (("School", "UserAdded"), "approved-change-key", "categories changed"),
        (("School", "UserAdded"), "different-change-key", "changeKey changed"),
    ],
)
def test_executor_blocks_stale_approved_snapshot_without_writing(
    tmp_path: Path,
    live_categories: tuple[str, ...],
    live_change_key: str,
    reason: str,
) -> None:
    action = make_action()
    repository, _ = approved_repository(tmp_path, action)
    graph = FakeGraphClient(
        live_snapshot(action, categories=live_categories, change_key=live_change_key)
    )

    report = execute(repository, action, graph)

    assert report.outcome is ActionGraphExecutionOutcome.CONFLICT
    assert report.final_status is MailboxActionStatus.FAILED
    assert reason in (report.reason or "")
    assert report.graph_write_request_count == 0
    assert graph.write_requests == []


def test_executor_requires_change_key_in_the_approved_snapshot(tmp_path: Path) -> None:
    action = make_action(change_key=None)
    repository, _ = approved_repository(tmp_path, action)
    graph = FakeGraphClient(live_snapshot(action))

    report = execute(repository, action, graph)

    assert report.outcome is ActionGraphExecutionOutcome.CONFLICT
    assert "has no changeKey" in (report.reason or "")
    assert graph.write_requests == []


def test_executor_completes_without_patch_when_live_state_already_matches(tmp_path: Path) -> None:
    initial = make_action()
    action = make_action(categories=("School", *initial.write_plan.managed_categories))
    repository, _ = approved_repository(tmp_path, action)
    graph = FakeGraphClient(live_snapshot(action))

    report = execute(repository, action, graph)

    assert report.outcome is ActionGraphExecutionOutcome.NO_CHANGE
    assert report.final_status is MailboxActionStatus.SUCCEEDED
    assert report.graph_write_request_count == 0
    assert graph.write_requests == []


def test_executor_records_preflight_failure_without_writing(tmp_path: Path) -> None:
    action = make_action()
    repository, _ = approved_repository(tmp_path, action)
    graph = FakeGraphClient(
        live_snapshot(action),
        read_error=GraphServiceError("preflight unavailable"),
    )

    report = execute(repository, action, graph)

    assert report.outcome is ActionGraphExecutionOutcome.FAILED
    assert report.graph_read_request_count == 1
    assert report.graph_write_request_count == 0
    assert "preflight unavailable" in (report.reason or "")


def test_executor_records_definite_write_failure_without_automatic_retry(tmp_path: Path) -> None:
    action = make_action()
    repository, _ = approved_repository(tmp_path, action)
    graph = FakeGraphClient(
        live_snapshot(action),
        write_error=GraphServiceError("write rejected"),
    )

    report = execute(repository, action, graph)

    assert report.outcome is ActionGraphExecutionOutcome.FAILED
    assert report.graph_write_request_count == 1
    assert len(graph.write_requests) == 1
    stored = repository.load().find(action.action_id)
    assert stored is not None
    assert stored.status is MailboxActionStatus.FAILED


def test_unknown_write_outcome_blocks_blind_retry(tmp_path: Path) -> None:
    action = make_action()
    repository, _ = approved_repository(tmp_path, action)
    graph = FakeGraphClient(
        live_snapshot(action),
        write_error=GraphWriteOutcomeUnknownError("connection lost after PATCH"),
    )

    report = execute(repository, action, graph)

    assert report.outcome is ActionGraphExecutionOutcome.OUTCOME_UNKNOWN
    assert report.final_status is MailboxActionStatus.OUTCOME_UNKNOWN
    assert len(graph.write_requests) == 1
    with pytest.raises(ActionExecutionGuardError, match="cannot be executed"):
        execute(repository, action, graph)
    assert len(graph.write_requests) == 1


def test_already_succeeded_action_is_a_zero_request_noop(tmp_path: Path) -> None:
    action = make_action()
    repository, _ = approved_repository(tmp_path, action)
    first_graph = FakeGraphClient(live_snapshot(action))
    execute(repository, action, first_graph)
    second_graph = FakeGraphClient(live_snapshot(action))

    report = execute(repository, action, second_graph)

    assert report.outcome is ActionGraphExecutionOutcome.ALREADY_SUCCEEDED
    assert report.graph_read_request_count == 0
    assert report.graph_write_request_count == 0
    assert second_graph.read_message_ids == []
    assert second_graph.write_requests == []


def test_pending_action_cannot_reach_graph(tmp_path: Path) -> None:
    action = make_action()
    repository = ActionQueueRepository(tmp_path / "action_queue.json")
    repository.enqueue((action,))
    graph = FakeGraphClient(live_snapshot(action))

    with pytest.raises(ActionExecutionGuardError, match="cannot be executed"):
        execute(repository, action, graph)
    assert graph.read_message_ids == []
    assert graph.write_requests == []


def test_successful_graph_write_with_queue_failure_is_not_reported_as_retryable(
    tmp_path: Path,
) -> None:
    class FailingCompletionRepository(ActionQueueRepository):
        def complete_execution(self, action_id: str, idempotency_key: str) -> MailboxAction:
            raise ActionQueueStorageError("disk unavailable")

    action = make_action()
    current_time = [CREATED_AT + timedelta(minutes=1)]
    repository = FailingCompletionRepository(
        tmp_path / "action_queue.json",
        clock=lambda: current_time[0],
    )
    repository.enqueue((action,))
    repository.transition(
        action.action_id,
        MailboxActionStatus.APPROVED,
        actor=ActionActor.USER,
    )
    graph = FakeGraphClient(live_snapshot(action))

    with pytest.raises(ActionExecutionPersistenceError, match="queue success"):
        execute(repository, action, graph)
    assert len(graph.write_requests) == 1
    stored = repository.load().find(action.action_id)
    assert stored is not None
    assert stored.status is MailboxActionStatus.WRITE_IN_FLIGHT


def test_executor_sends_no_patch_when_in_flight_state_cannot_be_persisted(
    tmp_path: Path,
) -> None:
    class FailingInFlightRepository(ActionQueueRepository):
        def mark_write_in_flight(
            self,
            action_id: str,
            idempotency_key: str,
        ) -> MailboxAction:
            raise ActionQueueStorageError("disk unavailable")

    action = make_action()
    repository = FailingInFlightRepository(tmp_path / "action_queue.json")
    repository.enqueue((action,))
    repository.transition(
        action.action_id,
        MailboxActionStatus.APPROVED,
        actor=ActionActor.USER,
    )
    graph = FakeGraphClient(live_snapshot(action))

    with pytest.raises(ActionExecutionPersistenceError, match="no Graph PATCH"):
        execute(repository, action, graph)
    assert graph.write_requests == []


def test_audit_failure_after_claim_stops_before_graph(tmp_path: Path) -> None:
    class FailingAuditLog:
        def append_unique(
            self,
            events: tuple[ActionAuditEvent, ...],
        ) -> AuditAppendReport:
            raise ActionAuditStorageError("disk unavailable")

    action = make_action()
    repository, _ = approved_repository(tmp_path, action)
    graph = FakeGraphClient(live_snapshot(action))
    assert action.idempotency_key is not None
    executor = ApprovedActionGraphExecutor(repository, graph, FailingAuditLog())

    with pytest.raises(ActionExecutionAuditError, match="execution claim"):
        executor.execute(
            action.action_id,
            action.idempotency_key,
            GraphAccessToken("secret-token"),
        )
    assert graph.read_message_ids == []
    assert graph.write_requests == []
    stored = repository.load().find(action.action_id)
    assert stored is not None
    assert stored.status is MailboxActionStatus.EXECUTING


def uncertain_repository(
    tmp_path: Path,
    action: MailboxAction,
    *,
    unknown: bool,
) -> ActionQueueRepository:
    repository, _ = approved_repository(tmp_path, action)
    assert action.idempotency_key is not None
    repository.claim_execution(action.action_id, action.idempotency_key)
    repository.mark_write_in_flight(action.action_id, action.idempotency_key)
    if unknown:
        repository.mark_execution_unknown(
            action.action_id,
            action.idempotency_key,
            note="PATCH response was not received",
        )
    return repository


def reconcile(
    repository: ActionQueueRepository,
    action: MailboxAction,
    graph: FakeGraphClient,
):
    assert action.idempotency_key is not None
    audit_log = ActionAuditLog(repository.path.with_name("actions.jsonl"))
    return UncertainActionReconciler(repository, graph, audit_log).reconcile(
        action.action_id,
        action.idempotency_key,
        GraphAccessToken("secret-token"),
    )


@pytest.mark.parametrize("unknown", [False, True])
def test_reconciliation_marks_intended_live_categories_as_applied(
    tmp_path: Path,
    unknown: bool,
) -> None:
    action = make_action(categories=("School",))
    repository = uncertain_repository(tmp_path, action, unknown=unknown)
    intended = ("School", *action.write_plan.managed_categories)
    graph = FakeGraphClient(live_snapshot(action, categories=intended, change_key="new-key"))

    report = reconcile(repository, action, graph)

    assert report.outcome is ActionReconciliationOutcome.APPLIED
    assert report.final_status is MailboxActionStatus.SUCCEEDED
    assert report.graph_read_request_count == 1
    assert report.graph_write_request_count == 0
    assert graph.write_requests == []
    events = ActionAuditLog(repository.path.with_name("actions.jsonl")).load()
    assert events[-2].to_status is MailboxActionStatus.SUCCEEDED
    assert events[-1].graph_operation is not None
    assert events[-1].graph_operation.operation is AuditGraphOperation.RECONCILE
    assert events[-1].graph_operation.outcome is AuditGraphOutcome.APPLIED
    assert events[-1].graph_operation.graph_write_request_count == 0


def test_reconciliation_marks_original_live_categories_as_not_applied(tmp_path: Path) -> None:
    action = make_action()
    repository = uncertain_repository(tmp_path, action, unknown=True)
    graph = FakeGraphClient(live_snapshot(action, change_key="possibly-changed"))

    report = reconcile(repository, action, graph)

    assert report.outcome is ActionReconciliationOutcome.NOT_APPLIED
    assert report.final_status is MailboxActionStatus.FAILED
    assert report.graph_write_request_count == 0
    assert graph.write_requests == []


def test_reconciliation_marks_third_state_as_conflict_without_writing(tmp_path: Path) -> None:
    action = make_action()
    repository = uncertain_repository(tmp_path, action, unknown=False)
    graph = FakeGraphClient(
        live_snapshot(action, categories=("School", "UserChanged"), change_key="other-key")
    )

    report = reconcile(repository, action, graph)

    assert report.outcome is ActionReconciliationOutcome.CONFLICT
    assert report.final_status is MailboxActionStatus.FAILED
    assert graph.write_requests == []


def test_failed_reconciliation_read_preserves_unknown_state(tmp_path: Path) -> None:
    action = make_action()
    repository = uncertain_repository(tmp_path, action, unknown=True)
    graph = FakeGraphClient(
        live_snapshot(action),
        read_error=GraphServiceError("temporarily unavailable"),
    )

    report = reconcile(repository, action, graph)

    assert report.outcome is ActionReconciliationOutcome.READ_FAILED
    assert report.final_status is MailboxActionStatus.OUTCOME_UNKNOWN
    assert graph.write_requests == []
    stored = repository.load().find(action.action_id)
    assert stored is not None
    assert stored.status is MailboxActionStatus.OUTCOME_UNKNOWN
    operation = ActionAuditLog(repository.path.with_name("actions.jsonl")).load()[-1]
    assert operation.graph_operation is not None
    assert operation.graph_operation.outcome is AuditGraphOutcome.READ_FAILED
    assert operation.graph_operation.graph_write_request_count == 0


def test_reconciliation_rejects_non_uncertain_action_before_graph(tmp_path: Path) -> None:
    action = make_action()
    repository, _ = approved_repository(tmp_path, action)
    graph = FakeGraphClient(live_snapshot(action))

    with pytest.raises(ActionExecutionGuardError, match="does not require"):
        reconcile(repository, action, graph)
    assert graph.read_message_ids == []
    assert graph.write_requests == []
