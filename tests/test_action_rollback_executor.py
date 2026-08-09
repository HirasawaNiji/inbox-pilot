"""Tests for real controlled rollback and zero-write reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from inbox_agent.actions import (
    ActionActor,
    ActionAuditLog,
    ActionQueueRepository,
    ControlledRollbackExecutor,
    MailboxAction,
    MailboxActionStatus,
    RollbackExecutionOutcome,
    RollbackReconciliationOutcome,
    UncertainRollbackReconciler,
    build_review_actions,
    build_rollback_dry_run,
)
from inbox_agent.graph import (
    GraphAccessToken,
    GraphCategoryWriteRequest,
    GraphCategoryWriteResult,
    GraphMessageCategorySnapshot,
    GraphWriteOutcomeUnknownError,
)
from inbox_agent.loader import load_dataset
from inbox_agent.pipeline import OfflinePipeline

ROOT = Path(__file__).resolve().parents[1]
CREATED_AT = datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
TOKEN = GraphAccessToken("secret-token")


def make_action() -> MailboxAction:
    dataset = load_dataset(ROOT / "data" / "samples" / "sample_emails.json")
    message = dataset.messages[0].model_copy(
        update={
            "categories": ("School", "InboxPilot/P5", "InboxPilot/old_notice"),
            "change_key": "before-forward",
        }
    )
    dataset = dataset.model_copy(update={"messages": (message,)})
    analysis = OfflinePipeline.from_yaml(ROOT / "config" / "rules.yaml").analyze_dataset(
        dataset,
        evaluated_at=CREATED_AT,
    )
    return build_review_actions(dataset, analysis)[0]


def succeeded_repository(tmp_path: Path) -> tuple[ActionQueueRepository, MailboxAction, str]:
    action = make_action()
    repository = ActionQueueRepository(
        tmp_path / "action_queue.json",
        clock=lambda: CREATED_AT + timedelta(minutes=1),
    )
    repository.enqueue((action,))
    repository.transition(
        action.action_id,
        MailboxActionStatus.APPROVED,
        actor=ActionActor.USER,
    )
    assert action.idempotency_key is not None
    repository.claim_execution(action.action_id, action.idempotency_key)
    repository.complete_execution(action.action_id, action.idempotency_key)
    plan = build_rollback_dry_run(
        repository.load(),
        action.action_id,
        repository.path,
        reason="Incorrect classification",
    ).plan
    return repository, action, plan.rollback_idempotency_key


class FakeGraphClient:
    def __init__(
        self,
        snapshot: GraphMessageCategorySnapshot,
        *,
        write_error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.write_error = write_error
        self.reads: list[str] = []
        self.writes: list[GraphCategoryWriteRequest] = []

    def get_category_snapshot(
        self,
        message_id: str,
        token: GraphAccessToken,
    ) -> GraphMessageCategorySnapshot:
        self.reads.append(message_id)
        return self.snapshot

    def set_categories(
        self,
        request: GraphCategoryWriteRequest,
        token: GraphAccessToken,
    ) -> GraphCategoryWriteResult:
        self.writes.append(request)
        if self.write_error is not None:
            raise self.write_error
        return GraphCategoryWriteResult(
            message_id=request.message_id,
            categories=request.categories,
            change_key="after-rollback",
        )


def live(action: MailboxAction, categories: tuple[str, ...]) -> GraphMessageCategorySnapshot:
    return GraphMessageCategorySnapshot(
        message_id=action.message_id,
        categories=categories,
        change_key="after-forward",
    )


def executor(
    repository: ActionQueueRepository,
    graph: FakeGraphClient,
) -> ControlledRollbackExecutor:
    return ControlledRollbackExecutor(
        repository,
        graph,
        ActionAuditLog(repository.path.with_name("actions.jsonl")),
    )


def test_rollback_preserves_live_user_categories_and_restores_managed_snapshot(
    tmp_path: Path,
) -> None:
    repository, action, key = succeeded_repository(tmp_path)
    current = ("School", "AddedLater", *action.write_plan.managed_categories)
    graph = FakeGraphClient(live(action, current))

    report = executor(repository, graph).execute(
        action.action_id,
        key,
        "Incorrect classification",
        TOKEN,
    )

    assert report.outcome is RollbackExecutionOutcome.ROLLED_BACK
    assert report.final_status is MailboxActionStatus.ROLLED_BACK
    assert report.graph_read_request_count == 1
    assert report.graph_write_request_count == 1
    assert graph.writes[0].categories == (
        "School",
        "AddedLater",
        "InboxPilot/P5",
        "InboxPilot/old_notice",
    )
    stored = repository.load().find(action.action_id)
    assert stored is not None
    assert stored.rollback_snapshot is not None
    assert stored.rollback_snapshot.observed_change_key == "after-forward"


def test_rollback_blocks_changed_managed_categories_without_patch(tmp_path: Path) -> None:
    repository, action, key = succeeded_repository(tmp_path)
    graph = FakeGraphClient(live(action, ("School", "InboxPilot/user_changed")))

    report = executor(repository, graph).execute(
        action.action_id,
        key,
        "Incorrect classification",
        TOKEN,
    )

    assert report.outcome is RollbackExecutionOutcome.CONFLICT
    assert report.final_status is MailboxActionStatus.ROLLBACK_FAILED
    assert report.graph_write_request_count == 0
    assert graph.writes == []


def test_uncertain_rollback_requires_read_only_reconciliation(tmp_path: Path) -> None:
    repository, action, key = succeeded_repository(tmp_path)
    current = ("School", *action.write_plan.managed_categories)
    first_graph = FakeGraphClient(
        live(action, current),
        write_error=GraphWriteOutcomeUnknownError("connection lost after PATCH"),
    )
    first = executor(repository, first_graph).execute(
        action.action_id,
        key,
        "Incorrect classification",
        TOKEN,
    )
    assert first.outcome is RollbackExecutionOutcome.OUTCOME_UNKNOWN

    target = ("School", "InboxPilot/P5", "InboxPilot/old_notice")
    reconciliation_graph = FakeGraphClient(live(action, target))
    report = UncertainRollbackReconciler(
        repository,
        reconciliation_graph,
        ActionAuditLog(repository.path.with_name("actions.jsonl")),
    ).reconcile(action.action_id, key, TOKEN)

    assert report.outcome is RollbackReconciliationOutcome.APPLIED
    assert report.final_status is MailboxActionStatus.ROLLED_BACK
    assert report.graph_write_request_count == 0
    assert reconciliation_graph.writes == []


def test_completed_rollback_replay_sends_zero_graph_requests(tmp_path: Path) -> None:
    repository, action, key = succeeded_repository(tmp_path)
    current = ("School", *action.write_plan.managed_categories)
    executor(repository, FakeGraphClient(live(action, current))).execute(
        action.action_id,
        key,
        "Incorrect classification",
        TOKEN,
    )
    replay_graph = FakeGraphClient(live(action, current))

    report = executor(repository, replay_graph).execute(
        action.action_id,
        key,
        "Incorrect classification",
        TOKEN,
    )

    assert report.outcome is RollbackExecutionOutcome.ALREADY_ROLLED_BACK
    assert report.graph_read_request_count == 0
    assert report.graph_write_request_count == 0
    assert replay_graph.reads == []
    assert replay_graph.writes == []
