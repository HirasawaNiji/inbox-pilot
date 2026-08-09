"""Private SQLite persistence for durable InboxPilot workflows."""

from inbox_agent.storage.database import Database, create_sqlite_engine, sqlite_url
from inbox_agent.storage.migrations import current_revision, head_revision, upgrade_database
from inbox_agent.storage.repository import (
    AnalysisRepository,
    MailboxActionRepository,
    MessageRepository,
    ServiceStateRecord,
    ServiceStateRepository,
    StorageCounts,
    StorageError,
    SyncCursorRepository,
    UpsertOutcome,
    UpsertResult,
    WorkflowRunRecord,
    WorkflowRunRepository,
    storage_counts,
)

__all__ = [
    "AnalysisRepository",
    "Database",
    "MailboxActionRepository",
    "MessageRepository",
    "ServiceStateRecord",
    "ServiceStateRepository",
    "StorageCounts",
    "StorageError",
    "SyncCursorRepository",
    "UpsertOutcome",
    "UpsertResult",
    "WorkflowRunRecord",
    "WorkflowRunRepository",
    "create_sqlite_engine",
    "current_revision",
    "head_revision",
    "sqlite_url",
    "storage_counts",
    "upgrade_database",
]
