"""Durable, privacy-safe delivery claims for duplicate prevention."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from inbox_agent.notifications.models import (
    NotificationDeliveryStatus,
    NotificationKind,
)
from inbox_agent.storage import Database
from inbox_agent.storage.orm import NotificationDeliveryRow


@dataclass(frozen=True, slots=True)
class NotificationDeliveryRecord:
    """Primitive delivery ledger record suitable for diagnostics and tests."""

    dedupe_key: str
    kind: str
    status: str
    related_hash: str | None
    attempt_count: int
    created_at: str
    last_attempt_at: str
    delivered_at: str | None
    error_summary: str | None


class NotificationDeliveryRepository:
    """Claim and complete deliveries without storing email content."""

    def __init__(self, database: Database, *, retry_limit: int = 3) -> None:
        if retry_limit < 1:
            raise ValueError("retry_limit must be positive")
        self.database = database
        self.retry_limit = retry_limit

    def claim(
        self,
        *,
        dedupe_key: str,
        kind: NotificationKind,
        attempted_at: datetime,
        related_hash: str | None = None,
    ) -> bool:
        """Atomically claim a new or retryable failed event."""

        if len(dedupe_key) != 64:
            raise ValueError("dedupe_key must be a SHA-256 digest")
        timestamp = attempted_at.isoformat()
        with self.database.session() as session:
            row = session.get(NotificationDeliveryRow, dedupe_key)
            if row is None:
                session.add(
                    NotificationDeliveryRow(
                        dedupe_key=dedupe_key,
                        kind=kind.value,
                        status=NotificationDeliveryStatus.PENDING.value,
                        related_hash=related_hash,
                        attempt_count=1,
                        created_at=timestamp,
                        last_attempt_at=timestamp,
                        delivered_at=None,
                        error_summary=None,
                    )
                )
                return True
            if row.status != NotificationDeliveryStatus.FAILED.value:
                return False
            if row.attempt_count >= self.retry_limit:
                return False
            row.status = NotificationDeliveryStatus.PENDING.value
            row.attempt_count += 1
            row.last_attempt_at = timestamp
            row.error_summary = None
            return True

    def mark_delivered(self, dedupe_keys: tuple[str, ...], delivered_at: datetime) -> None:
        """Complete claimed events after their shared delivery succeeds."""

        if not dedupe_keys:
            return
        timestamp = delivered_at.isoformat()
        with self.database.session() as session:
            for key in dedupe_keys:
                row = session.get(NotificationDeliveryRow, key)
                if row is None:
                    raise LookupError(f"notification claim does not exist: {key}")
                row.status = NotificationDeliveryStatus.DELIVERED.value
                row.delivered_at = timestamp
                row.error_summary = None

    def mark_failed(
        self,
        dedupe_keys: tuple[str, ...],
        *,
        failed_at: datetime,
        error_summary: str,
    ) -> None:
        """Keep a bounded safe failure reason for later retry."""

        if not dedupe_keys:
            return
        bounded = error_summary[:500] or "notification delivery failed"
        timestamp = failed_at.isoformat()
        with self.database.session() as session:
            for key in dedupe_keys:
                row = session.get(NotificationDeliveryRow, key)
                if row is None:
                    raise LookupError(f"notification claim does not exist: {key}")
                row.status = NotificationDeliveryStatus.FAILED.value
                row.last_attempt_at = timestamp
                row.error_summary = bounded

    def get(self, dedupe_key: str) -> NotificationDeliveryRecord | None:
        with self.database.session() as session:
            row = session.get(NotificationDeliveryRow, dedupe_key)
            if row is None:
                return None
            return NotificationDeliveryRecord(
                dedupe_key=row.dedupe_key,
                kind=row.kind,
                status=row.status,
                related_hash=row.related_hash,
                attempt_count=row.attempt_count,
                created_at=row.created_at,
                last_attempt_at=row.last_attempt_at,
                delivered_at=row.delivered_at,
                error_summary=row.error_summary,
            )
