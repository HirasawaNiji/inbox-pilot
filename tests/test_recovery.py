"""Stage 4 step 7 consistent backup and controlled restore tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from inbox_agent.actions.locking import ActionFileLock
from inbox_agent.observability import (
    EventOutcome,
    ObservabilityEvent,
    ObservabilityRecorder,
    safe_message_hash,
)
from inbox_agent.observability.recovery import (
    RecoveryError,
    create_database_backup,
    restore_database_backup,
)
from inbox_agent.storage import Database, current_revision, upgrade_database

NOW = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
RUN_ID = "run-00000000000000000000000000000001"


def record_message(database_path: Path, message_id: str) -> None:
    database = Database(database_path)
    try:
        ObservabilityRecorder(database).record(
            ObservabilityEvent(
                occurred_at=NOW,
                run_id=RUN_ID,
                message_hash=safe_message_hash(message_id),
                component="pipeline",
                operation="message_analysis",
                outcome=EventOutcome.SUCCEEDED,
            )
        )
    finally:
        database.dispose()


def test_backup_manifest_and_restore_are_verified_and_gated(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox.sqlite3"
    backup_dir = tmp_path / "private" / "backups"
    lock_path = tmp_path / "private" / "service.lock"
    upgrade_database(database_path)
    record_message(database_path, "first-message")

    backup = create_database_backup(database_path, backup_dir, now=NOW)
    manifest = json.loads(backup.manifest_path.read_text(encoding="utf-8"))
    assert backup.backup_path.is_file()
    assert backup.integrity_check == "ok"
    assert backup.revision == "0005_observability"
    assert manifest["sha256"] == backup.sha256
    assert manifest["size_bytes"] == backup.size_bytes

    record_message(database_path, "second-message")
    with pytest.raises(RecoveryError, match="explicit confirmation"):
        restore_database_backup(
            backup.backup_path,
            database_path,
            backup_dir=backup_dir,
            lock_path=lock_path,
            confirmed=False,
            now=NOW,
        )
    with ActionFileLock(lock_path, timeout_seconds=0):
        with pytest.raises(RecoveryError, match="scheduler is active"):
            restore_database_backup(
                backup.backup_path,
                database_path,
                backup_dir=backup_dir,
                lock_path=lock_path,
                confirmed=True,
                now=NOW,
            )

    wal_path = Path(f"{database_path}-wal")
    shm_path = Path(f"{database_path}-shm")
    wal_path.write_bytes(b"stale wal")
    shm_path.write_bytes(b"stale shm")
    restored = restore_database_backup(
        backup.backup_path,
        database_path,
        backup_dir=backup_dir,
        lock_path=lock_path,
        confirmed=True,
        now=NOW,
    )
    database = Database(database_path)
    try:
        first = ObservabilityRecorder(database).trace_message(safe_message_hash("first-message"))
        second = ObservabilityRecorder(database).trace_message(safe_message_hash("second-message"))
        revision = current_revision(database.engine)
    finally:
        database.dispose()

    assert restored.integrity_check == "ok"
    assert restored.revision == "0005_observability"
    assert restored.pre_restore_backup is not None
    assert restored.pre_restore_backup.is_file()
    assert not wal_path.exists()
    assert not shm_path.exists()
    assert revision == "0005_observability"
    assert len(first) == 1
    assert second == ()


def test_restore_rejects_corrupt_backup_before_touching_destination(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox.sqlite3"
    corrupt_path = tmp_path / "private" / "corrupt.sqlite3"
    upgrade_database(database_path)
    original = database_path.read_bytes()
    corrupt_path.write_bytes(b"not a sqlite database")

    with pytest.raises(RecoveryError, match="valid SQLite backup"):
        restore_database_backup(
            corrupt_path,
            database_path,
            backup_dir=tmp_path / "backups",
            lock_path=tmp_path / "service.lock",
            confirmed=True,
        )
    assert database_path.read_bytes() == original
