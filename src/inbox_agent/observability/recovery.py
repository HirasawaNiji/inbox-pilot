"""Consistent SQLite backup and explicitly gated restore operations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import Field, field_validator

from inbox_agent.models import FrozenModel
from inbox_agent.service.status import service_is_active
from inbox_agent.storage import Database, current_revision, head_revision


class RecoveryError(RuntimeError):
    """Raised when a backup or restore safety check fails."""


class BackupReport(FrozenModel):
    """Verified private database backup plus integrity metadata."""

    database_path: Path
    backup_path: Path
    manifest_path: Path
    created_at: datetime
    revision: str | None
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1)
    integrity_check: str

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("backup timestamps must include timezone information")
        return value


class RestoreReport(FrozenModel):
    """Verified restore result with the automatic pre-restore backup."""

    database_path: Path
    restored_from: Path
    pre_restore_backup: Path | None
    restored_at: datetime
    revision: str | None
    integrity_check: str


def create_database_backup(
    database_path: Path,
    output_dir: Path,
    *,
    now: datetime | None = None,
    label: str = "inbox-pilot",
) -> BackupReport:
    """Use SQLite's online backup API so WAL state is copied consistently."""

    source_path = database_path.resolve()
    if not source_path.is_file():
        raise RecoveryError(f"database does not exist: {source_path}")
    created_at = now or datetime.now(UTC)
    stamp = created_at.strftime("%Y%m%dT%H%M%S%fZ")
    target_dir = output_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_path = target_dir / f"{label}-{stamp}.sqlite3"
    temporary_path = target_dir / f".{backup_path.name}.{uuid4().hex}.tmp"
    try:
        _sqlite_copy(source_path, temporary_path)
        integrity = _integrity_check(temporary_path)
        if integrity != "ok":
            raise RecoveryError("backup integrity_check failed")
        os.replace(temporary_path, backup_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    revision = _revision(backup_path)
    digest = _sha256(backup_path)
    manifest_path = backup_path.with_suffix(".manifest.json")
    manifest = {
        "schema_version": "1.0",
        "created_at": created_at.isoformat(),
        "database_filename": backup_path.name,
        "revision": revision,
        "sha256": digest,
        "size_bytes": backup_path.stat().st_size,
        "integrity_check": integrity,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return BackupReport(
        database_path=source_path,
        backup_path=backup_path,
        manifest_path=manifest_path,
        created_at=created_at,
        revision=revision,
        sha256=digest,
        size_bytes=backup_path.stat().st_size,
        integrity_check=integrity,
    )


def restore_database_backup(
    backup_path: Path,
    database_path: Path,
    *,
    backup_dir: Path,
    lock_path: Path,
    confirmed: bool,
    now: datetime | None = None,
) -> RestoreReport:
    """Restore only after confirmation, lock probing, validation, and safety backup."""

    if not confirmed:
        raise RecoveryError("restore requires explicit confirmation")
    source_path = backup_path.resolve()
    target_path = database_path.resolve()
    if not source_path.is_file():
        raise RecoveryError(f"backup does not exist: {source_path}")
    if service_is_active(lock_path.resolve()):
        raise RecoveryError("the local scheduler is active; stop it before restoring")
    if _integrity_check(source_path) != "ok":
        raise RecoveryError("backup integrity_check failed")
    source_revision = _revision(source_path)
    expected_revision = head_revision(source_path)
    if source_revision != expected_revision:
        raise RecoveryError(
            "backup revision "
            f"{source_revision or 'uninitialized'} does not match {expected_revision}"
        )

    pre_restore: BackupReport | None = None
    if target_path.is_file():
        pre_restore = create_database_backup(
            target_path,
            backup_dir / "pre-restore",
            now=now,
            label="pre-restore",
        )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target_path.parent / f".{target_path.name}.{uuid4().hex}.restore.tmp"
    try:
        _sqlite_copy(source_path, temporary_path)
        if _integrity_check(temporary_path) != "ok":
            raise RecoveryError("restored temporary database failed integrity_check")
        _remove_sqlite_sidecars(target_path)
        os.replace(temporary_path, target_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    restored_at = now or datetime.now(UTC)
    return RestoreReport(
        database_path=target_path,
        restored_from=source_path,
        pre_restore_backup=pre_restore.backup_path if pre_restore is not None else None,
        restored_at=restored_at,
        revision=_revision(target_path),
        integrity_check=_integrity_check(target_path),
    )


def _sqlite_copy(source: Path, target: Path) -> None:
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
        with closing(sqlite3.connect(target)) as target_connection:
            source_connection.backup(target_connection)


def _integrity_check(path: Path) -> str:
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as error:
        raise RecoveryError("file is not a valid SQLite backup") from error
    return str(row[0]) if row else "missing result"


def _revision(path: Path) -> str | None:
    database = Database(path)
    try:
        return current_revision(database.engine)
    finally:
        database.dispose()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_sqlite_sidecars(database_path: Path) -> None:
    """Prevent a stale WAL or rollback journal from being applied to restored data."""

    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{database_path}{suffix}").unlink(missing_ok=True)
