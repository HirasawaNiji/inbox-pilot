"""Stage 4 SQLite migration and repository tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from inbox_agent.actions import build_review_actions
from inbox_agent.loader import load_dataset
from inbox_agent.models import MailSource
from inbox_agent.normalizer import normalize_message
from inbox_agent.pipeline import OfflinePipeline
from inbox_agent.storage import (
    AnalysisRepository,
    Database,
    MailboxActionRepository,
    MessageRepository,
    SyncCursorRepository,
    UpsertOutcome,
    current_revision,
    storage_counts,
    upgrade_database,
)

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
POLICY_PATH = ROOT / "config" / "rules.yaml"


def migrated_database(tmp_path: Path) -> Database:
    path = tmp_path / "private" / "inbox_pilot.sqlite3"
    upgrade_database(path)
    return Database(path)


def test_upgrade_creates_versioned_schema_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "private" / "inbox_pilot.sqlite3"

    upgrade_database(path)
    upgrade_database(path)

    database = Database(path)
    try:
        assert current_revision(database.engine) == "0005_observability"
        assert storage_counts(database).messages == 0
    finally:
        database.dispose()


def test_workflow_migration_upgrades_existing_step_one_database(tmp_path: Path) -> None:
    path = tmp_path / "private" / "inbox_pilot.sqlite3"
    upgrade_database(path, "0001_stage4")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO messages (
                source, source_id, internet_message_id, subject, from_address,
                received_at, change_key, content_hash, payload_json, normalized_json,
                created_at, updated_at
            ) VALUES (?, ?, NULL, ?, ?, ?, NULL, ?, ?, NULL, ?, ?)
            """,
            (
                "mock",
                "existing-message",
                "Existing",
                "sender@example.edu",
                "2026-08-09T12:00:00+00:00",
                "a" * 64,
                "{}",
                "2026-08-09T12:00:00+00:00",
                "2026-08-09T12:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO analyses (
                message_id, fingerprint, priority, category, decision_source,
                requires_review, policy_version, evaluated_at, triage_json,
                rule_json, llm_json, created_at
            ) VALUES (1, ?, 'P3', 'general_notice', 'rule', 0, 'rules-v1', ?, '{}', NULL, NULL, ?)
            """,
            (
                "b" * 64,
                "2026-08-09T12:00:00+00:00",
                "2026-08-09T12:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO workflow_runs (
                run_id, status, started_at, finished_at, counters_json, error_summary
            ) VALUES ('legacy-run', 'completed', ?, ?, '{}', NULL)
            """,
            ("2026-08-09T12:00:00+00:00", "2026-08-09T12:00:00+00:00"),
        )

    upgrade_database(path)

    database = Database(path)
    try:
        assert current_revision(database.engine) == "0005_observability"
    finally:
        database.dispose()
    with sqlite3.connect(path) as connection:
        analysis = connection.execute(
            "SELECT message_content_hash, analysis_profile, complete FROM analyses"
        ).fetchone()
        workflow = connection.execute(
            "SELECT current_step, steps_json FROM workflow_runs"
        ).fetchone()
    assert analysis == ("a" * 64, "0" * 64, 1)
    assert workflow == (None, "[]")


def test_service_migration_upgrades_existing_workflow_database(tmp_path: Path) -> None:
    path = tmp_path / "private" / "inbox_pilot.sqlite3"
    upgrade_database(path, "0002_workflow")

    upgrade_database(path)

    database = Database(path)
    try:
        assert current_revision(database.engine) == "0005_observability"
        with database.engine.connect() as connection:
            columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(service_states)")
            }
        assert {
            "service_name",
            "status",
            "pid",
            "next_run_at",
            "consecutive_failures",
        } <= columns
        with database.engine.connect() as connection:
            notification_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(notification_deliveries)")
            }
        assert {
            "dedupe_key",
            "kind",
            "status",
            "attempt_count",
            "delivered_at",
        } <= notification_columns
        with database.engine.connect() as connection:
            observability_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(observability_events)")
            }
        assert {
            "run_id",
            "message_hash",
            "operation",
            "duration_ms",
            "provider",
            "input_tokens",
            "estimated_cost_microusd",
        } <= observability_columns
    finally:
        database.dispose()


def test_message_repository_creates_restores_and_updates(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    original = load_dataset(DATASET_PATH).messages[0]
    repository = MessageRepository(database)
    try:
        created = repository.upsert(original)
        unchanged = repository.upsert(original)
        updated_message = original.model_copy(update={"subject": "Updated subject"})
        updated = repository.upsert(updated_message)
        normalized = normalize_message(updated_message)
        normalized_saved = repository.save_normalized(normalized)
        normalized_unchanged = repository.save_normalized(normalized)

        assert created.outcome is UpsertOutcome.CREATED
        assert unchanged == type(unchanged)(UpsertOutcome.UNCHANGED, created.row_id)
        assert updated == type(updated)(UpsertOutcome.UPDATED, created.row_id)
        assert repository.get(original.source, original.source_id) == updated_message
        assert normalized_saved is UpsertOutcome.UPDATED
        assert normalized_unchanged is UpsertOutcome.UNCHANGED
        assert repository.get_normalized(original.source, original.source_id) == normalized
        assert repository.list(limit=1) == (updated_message,)
    finally:
        database.dispose()


def test_analysis_and_action_snapshots_are_idempotent(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    dataset = load_dataset(DATASET_PATH)
    single_message_dataset = dataset.model_copy(update={"messages": dataset.messages[:1]})
    report = OfflinePipeline.from_yaml(POLICY_PATH).analyze_dataset(single_message_dataset)
    message = single_message_dataset.messages[0]
    result = report.results[0]
    rule_evaluation = report.rule_evaluations[0]
    action = build_review_actions(single_message_dataset, report)[0]
    message_repository = MessageRepository(database)
    analysis_repository = AnalysisRepository(database)
    action_repository = MailboxActionRepository(database)
    try:
        message_repository.upsert(message)
        first_analysis = analysis_repository.save(
            source=message.source,
            result=result,
            rule_evaluation=rule_evaluation,
        )
        repeated_analysis = analysis_repository.save(
            source=message.source,
            result=result,
            rule_evaluation=rule_evaluation,
        )
        first_action = action_repository.upsert(source=message.source, action=action)
        repeated_action = action_repository.upsert(source=message.source, action=action)

        assert first_analysis.outcome is UpsertOutcome.CREATED
        assert repeated_analysis.outcome is UpsertOutcome.UNCHANGED
        assert analysis_repository.latest(message.source, message.source_id) == result
        assert first_action.outcome is UpsertOutcome.CREATED
        assert repeated_action.outcome is UpsertOutcome.UNCHANGED
        assert action_repository.get(action.action_id) == action
        counts = storage_counts(database)
        assert (counts.messages, counts.analyses, counts.actions) == (1, 1, 1)
    finally:
        database.dispose()


def test_sync_cursor_is_scoped_and_updated_without_duplication(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    repository = SyncCursorRepository(database)
    scope = {
        "provider": MailSource.MICROSOFT_GRAPH.value,
        "mailbox_key": "personal",
        "folder_key": "inbox",
    }
    try:
        assert repository.set(**scope, cursor={"delta_link": "first"}) is UpsertOutcome.CREATED
        assert repository.set(**scope, cursor={"delta_link": "first"}) is UpsertOutcome.UNCHANGED
        assert repository.set(**scope, cursor={"delta_link": "second"}) is UpsertOutcome.UPDATED
        assert repository.get(**scope) == {"delta_link": "second"}
        assert storage_counts(database).sync_cursors == 1
    finally:
        database.dispose()
