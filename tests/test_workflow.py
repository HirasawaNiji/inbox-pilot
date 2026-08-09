"""Stage 4 durable workflow orchestration tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inbox_agent.graph import GraphAccessToken, GraphSyncReport
from inbox_agent.llm import FakeLLMProvider, LLMFusionEngine, LLMRouter
from inbox_agent.loader import load_dataset
from inbox_agent.models import LLMMessageAnalysis, MessageCategory, MessageDataset, Priority
from inbox_agent.pipeline import OfflinePipeline
from inbox_agent.storage import Database, WorkflowRunRepository, storage_counts, upgrade_database
from inbox_agent.workflow import (
    DatasetSyncResult,
    WorkflowExecutionError,
    WorkflowOrchestrator,
    WorkflowRuntimeSettings,
    WorkflowStatus,
    build_analysis_profile,
    execute_workflow,
)

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
POLICY_PATH = ROOT / "config" / "rules.yaml"
FIXED_TIME = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def build_orchestrator(
    tmp_path: Path,
    database: Database,
    *,
    pipeline: OfflinePipeline | None = None,
    profile: str | None = None,
    llm_provider: FakeLLMProvider | None = None,
    run_id: str = "run-00000000000000000000000000000001",
) -> WorkflowOrchestrator:
    return WorkflowOrchestrator(
        database=database,
        pipeline=pipeline or OfflinePipeline.from_yaml(POLICY_PATH),
        analysis_profile=profile or build_analysis_profile(POLICY_PATH),
        action_queue_path=tmp_path / "private" / "action_queue.json",
        audit_log_path=tmp_path / "private" / "audit" / "actions.jsonl",
        llm_provider=llm_provider,
        clock=lambda: FIXED_TIME,
        run_id_factory=lambda: run_id,
    )


def write_dataset(path: Path, dataset: MessageDataset) -> Path:
    path.write_text(
        json.dumps(dataset.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_workflow_skips_unchanged_messages_and_never_writes_graph(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox_pilot.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    dataset = load_dataset(DATASET_PATH).model_copy(
        update={"messages": load_dataset(DATASET_PATH).messages[:2]}
    )
    dataset_path = write_dataset(tmp_path / "messages.json", dataset)
    try:
        first = build_orchestrator(tmp_path, database).run(dataset_path)
        second = build_orchestrator(
            tmp_path,
            database,
            run_id="run-00000000000000000000000000000002",
        ).run(dataset_path)

        assert first.status is WorkflowStatus.COMPLETED
        assert first.imported_created == 2
        assert first.eligible_messages == 2
        assert first.analyzed_messages == 2
        assert first.actions_added == 2
        assert first.graph_write_request_count == 0
        assert second.imported_unchanged == 2
        assert second.eligible_messages == 0
        assert second.skipped_current == 2
        assert second.analyzed_messages == 0
        assert second.actions_generated == 0
        assert second.graph_write_request_count == 0
        counts = storage_counts(database)
        assert (counts.messages, counts.analyses, counts.actions, counts.workflow_runs) == (
            2,
            2,
            2,
            2,
        )
        latest = WorkflowRunRepository(database).latest()
        assert latest is not None
        assert latest.run_id.endswith("02")
        assert latest.status == WorkflowStatus.COMPLETED.value
        assert latest.current_step is None
        assert len(latest.steps) == 6
    finally:
        database.dispose()


def test_workflow_reanalyzes_only_changed_message(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox_pilot.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    dataset = load_dataset(DATASET_PATH).model_copy(
        update={"messages": load_dataset(DATASET_PATH).messages[:2]}
    )
    dataset_path = write_dataset(tmp_path / "messages.json", dataset)
    try:
        build_orchestrator(tmp_path, database).run(dataset_path)
        changed = dataset.messages[0].model_copy(update={"subject": "Changed subject"})
        changed_dataset = dataset.model_copy(update={"messages": (changed, dataset.messages[1])})
        write_dataset(dataset_path, changed_dataset)

        report = build_orchestrator(
            tmp_path,
            database,
            run_id="run-00000000000000000000000000000002",
        ).run(dataset_path)

        assert report.imported_updated == 1
        assert report.imported_unchanged == 1
        assert report.eligible_messages == 1
        assert report.skipped_current == 1
        assert report.analyzed_messages == 1
        assert storage_counts(database).analyses == 3
    finally:
        database.dispose()


def test_policy_change_creates_new_analysis_profile(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox_pilot.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    dataset = load_dataset(DATASET_PATH).model_copy(
        update={"messages": load_dataset(DATASET_PATH).messages[:1]}
    )
    dataset_path = write_dataset(tmp_path / "messages.json", dataset)
    changed_policy = tmp_path / "rules-v2.yaml"
    changed_policy.write_text(
        POLICY_PATH.read_text(encoding="utf-8").replace(
            "policy_version: rules-v1",
            "policy_version: rules-v2",
        ),
        encoding="utf-8",
    )
    try:
        first = build_orchestrator(tmp_path, database).run(dataset_path)
        second = build_orchestrator(
            tmp_path,
            database,
            pipeline=OfflinePipeline.from_yaml(changed_policy),
            profile=build_analysis_profile(changed_policy),
            run_id="run-00000000000000000000000000000002",
        ).run(dataset_path)

        assert first.analysis_profile != second.analysis_profile
        assert second.imported_unchanged == 1
        assert second.eligible_messages == 1
        assert second.analyzed_messages == 1
        assert storage_counts(database).analyses == 2
    finally:
        database.dispose()


def test_llm_failure_is_isolated_and_retried_without_action(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox_pilot.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    dataset = load_dataset(DATASET_PATH).model_copy(
        update={"messages": load_dataset(DATASET_PATH).messages[:2]}
    )
    dataset_path = write_dataset(tmp_path / "messages.json", dataset)
    first_id, failing_id = (message.source_id for message in dataset.messages)
    response = LLMMessageAnalysis(
        priority=Priority.P3,
        category=MessageCategory.COURSE_REGISTRATION,
        summary="Structured test result",
        action_items=(),
        deadline=None,
        confidence=0.95,
        rationale="Deterministic test response",
        requires_review=False,
    )
    provider = FakeLLMProvider(
        {first_id: response},
        failures={failing_id: "temporary provider outage"},
    )
    pipeline = OfflinePipeline.from_yaml(
        POLICY_PATH,
        llm_provider=provider,
        llm_router=LLMRouter.analyze_all(),
        llm_fusion=LLMFusionEngine(),
    )
    profile = build_analysis_profile(POLICY_PATH, llm_provider=provider)
    try:
        first = build_orchestrator(
            tmp_path,
            database,
            pipeline=pipeline,
            profile=profile,
            llm_provider=provider,
        ).run(dataset_path)
        second = build_orchestrator(
            tmp_path,
            database,
            pipeline=pipeline,
            profile=profile,
            llm_provider=provider,
            run_id="run-00000000000000000000000000000002",
        ).run(dataset_path)

        assert first.status is WorkflowStatus.COMPLETED_WITH_FAILURES
        assert first.analyzed_messages == 2
        assert len(first.llm_failures) == 1
        assert first.actions_generated == 1
        assert second.eligible_messages == 1
        assert second.skipped_current == 1
        assert len(second.llm_failures) == 1
        assert second.actions_generated == 0
        assert provider.calls == (first_id, failing_id, failing_id)
        assert storage_counts(database).analyses == 2
    finally:
        database.dispose()


def test_optional_read_only_sync_selects_returned_dataset(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox_pilot.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    dataset = load_dataset(DATASET_PATH).model_copy(
        update={"messages": load_dataset(DATASET_PATH).messages[:1]}
    )
    synchronized_path = write_dataset(tmp_path / "outlook.json", dataset)
    try:
        report = build_orchestrator(tmp_path, database).run(
            tmp_path / "unused.json",
            dataset_sync=lambda: DatasetSyncResult(
                dataset_path=synchronized_path,
                completed=True,
                created_count=1,
                updated_count=0,
                removed_count=0,
                unchanged_count=0,
                failure_count=0,
            ),
        )

        assert report.outlook_sync_requested is True
        assert report.dataset_path == synchronized_path
        assert report.total_messages == 1
        assert report.graph_write_request_count == 0
        assert report.steps[0].name == "outlook_sync"
        assert report.steps[0].processed_count == 1
    finally:
        database.dispose()


def test_fatal_failure_is_persisted_for_workflow_status(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox_pilot.sqlite3"
    upgrade_database(database_path)
    database = Database(database_path)
    orchestrator = build_orchestrator(tmp_path, database)
    try:
        with pytest.raises(WorkflowExecutionError) as captured:
            orchestrator.run(tmp_path / "missing.json")

        latest = WorkflowRunRepository(database).latest()
        assert latest is not None
        assert latest.run_id == captured.value.run_id
        assert latest.status == WorkflowStatus.FAILED.value
        assert latest.error_summary is not None
        assert latest.steps[-1]["status"] == "failed"
    finally:
        database.dispose()


def test_runtime_wires_optional_outlook_sync_as_read_only_dataset_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = load_dataset(DATASET_PATH).model_copy(
        update={"messages": load_dataset(DATASET_PATH).messages[:1]}
    )
    synchronized_path = write_dataset(tmp_path / "outlook.json", dataset)
    graph_config = tmp_path / "graph.local.yaml"
    graph_config.write_text(
        "client_id: 12345678-1234-4234-8234-123456789abc\n"
        "account_audience: consumers\n"
        "scopes: [Mail.Read]\n",
        encoding="utf-8",
    )
    calls: dict[str, bool] = {}

    class FakeProvider:
        @classmethod
        def from_settings(cls, *args: object, **kwargs: object) -> FakeProvider:
            return cls()

        def acquire_silent(self) -> GraphAccessToken:
            calls["authenticated"] = True
            return GraphAccessToken("secret-token")

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls["client_created"] = True

    class FakeSynchronizer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def sync(self, token: GraphAccessToken) -> GraphSyncReport:
            assert token.access_token == "secret-token"
            calls["synchronized"] = True
            return GraphSyncReport(
                started_from_delta=False,
                completed=True,
                pages_fetched=1,
                created_count=1,
                updated_count=0,
                removed_count=0,
                unchanged_count=0,
                total_messages=1,
                dataset_path=synchronized_path,
                state_path=tmp_path / "state.json",
            )

    monkeypatch.setattr("inbox_agent.workflow.runtime.GraphTokenProvider", FakeProvider)
    monkeypatch.setattr("inbox_agent.workflow.runtime.GraphMailClient", FakeClient)
    monkeypatch.setattr("inbox_agent.workflow.runtime.GraphInboxSynchronizer", FakeSynchronizer)

    report = execute_workflow(
        WorkflowRuntimeSettings(
            project_root=ROOT,
            dataset_path=tmp_path / "unused.json",
            database_path=tmp_path / "private" / "workflow.sqlite3",
            action_queue_path=tmp_path / "private" / "actions.json",
            audit_log_path=tmp_path / "private" / "audit.jsonl",
            policy_path=POLICY_PATH,
            sync_outlook=True,
            graph_config_path=graph_config,
        )
    )

    assert calls == {
        "authenticated": True,
        "client_created": True,
        "synchronized": True,
    }
    assert report.outlook_sync_requested is True
    assert report.total_messages == 1
    assert report.graph_write_request_count == 0
