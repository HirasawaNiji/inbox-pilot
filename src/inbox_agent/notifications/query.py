"""Read-only projections used by the notification and digest layer."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from inbox_agent.actions import ActionQueueRepository, MailboxActionStatus
from inbox_agent.models import Priority, TriageResult
from inbox_agent.notifications.models import DailyDigest, DigestItem
from inbox_agent.storage import Database
from inbox_agent.storage.orm import AnalysisRow, MessageRow

_PRIORITY_ORDER = {Priority.P1: 1, Priority.P2: 2, Priority.P3: 3, Priority.P4: 4, Priority.P5: 5}


class NotificationQueryService:
    """Build bounded projections without loading complete message bodies."""

    def __init__(self, database: Database, action_queue_path: Path) -> None:
        self.database = database
        self.action_queue_path = action_queue_path

    def latest_items(self, *, limit: int = 2_000) -> tuple[DigestItem, ...]:
        """Return the latest complete triage snapshot for each message."""

        latest = (
            select(
                AnalysisRow.message_id.label("message_id"),
                func.max(AnalysisRow.id).label("analysis_id"),
            )
            .where(AnalysisRow.complete == 1)
            .group_by(AnalysisRow.message_id)
            .subquery()
        )
        statement = (
            select(MessageRow, AnalysisRow)
            .join(latest, latest.c.message_id == MessageRow.id)
            .join(AnalysisRow, AnalysisRow.id == latest.c.analysis_id)
            .order_by(MessageRow.received_at.desc(), MessageRow.id.desc())
            .limit(limit)
        )
        with self.database.session() as session:
            rows = session.execute(statement).all()
        items = []
        for message, analysis in rows:
            triage = TriageResult.model_validate_json(analysis.triage_json)
            received_at = datetime.fromisoformat(message.received_at)
            if received_at.tzinfo is None or received_at.utcoffset() is None:
                raise ValueError("stored message received_at must include timezone information")
            items.append(
                DigestItem(
                    message_key=f"{message.source}:{message.source_id}",
                    analysis_fingerprint=analysis.fingerprint,
                    subject=message.subject,
                    priority=triage.priority,
                    summary=triage.summary,
                    received_at=received_at,
                    deadline=triage.deadline,
                    requires_review=triage.requires_review,
                )
            )
        return tuple(items)

    def priority_alert_items(self) -> tuple[DigestItem, ...]:
        """Return all current P1/P2 items; the delivery ledger removes repeats."""

        return tuple(
            item for item in self.latest_items() if item.priority in {Priority.P1, Priority.P2}
        )

    def deadline_alert_items(
        self,
        *,
        now: datetime,
        window_hours: int,
    ) -> tuple[DigestItem, ...]:
        """Return not-yet-expired items inside the configured reminder window."""

        end = now + timedelta(hours=window_hours)
        return tuple(
            sorted(
                (
                    item
                    for item in self.latest_items()
                    if item.deadline is not None and now <= item.deadline <= end
                ),
                key=lambda item: item.deadline or end,
            )
        )

    def daily_digest(
        self,
        *,
        now: datetime,
        lookback_hours: int,
        deadline_window_hours: int,
    ) -> DailyDigest:
        """Build a useful private digest without including complete message bodies."""

        items = self.latest_items()
        cutoff = now - timedelta(hours=lookback_hours)
        priorities = tuple(
            sorted(
                (
                    item
                    for item in items
                    if item.priority in {Priority.P1, Priority.P2, Priority.P3}
                    and item.received_at >= cutoff
                ),
                key=lambda item: (_PRIORITY_ORDER[item.priority], -item.received_at.timestamp()),
            )
        )
        deadline_end = now + timedelta(hours=deadline_window_hours)
        deadlines = tuple(
            sorted(
                (
                    item
                    for item in items
                    if item.deadline is not None and now <= item.deadline <= deadline_end
                ),
                key=lambda item: item.deadline or deadline_end,
            )
        )
        queue = ActionQueueRepository(self.action_queue_path).load()
        pending = sum(
            action.status is MailboxActionStatus.PENDING_REVIEW for action in queue.actions
        )
        approved = sum(action.status is MailboxActionStatus.APPROVED for action in queue.actions)
        return DailyDigest(
            generated_at=now,
            priority_items=priorities,
            deadline_items=deadlines,
            review_count=sum(item.requires_review for item in items),
            pending_action_count=pending,
            approved_action_count=approved,
        )
