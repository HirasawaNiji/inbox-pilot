"""Post-workflow coordination for deduplicated alerts and daily summaries."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from inbox_agent.models import Priority
from inbox_agent.notifications.desktop import DesktopNotifier, WindowsToastNotifier
from inbox_agent.notifications.models import (
    DigestItem,
    NotificationBatchReport,
    NotificationKind,
)
from inbox_agent.notifications.query import NotificationQueryService
from inbox_agent.notifications.repository import NotificationDeliveryRepository
from inbox_agent.notifications.summary import write_daily_digest
from inbox_agent.service.config import ServiceNotificationSettings
from inbox_agent.service.models import ServiceRunOutcome, ServiceRunResult
from inbox_agent.storage import Database

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class NotificationCoordinator:
    """Translate durable workflow state into private, non-duplicated local alerts."""

    def __init__(
        self,
        *,
        database: Database,
        action_queue_path: Path,
        output_dir: Path,
        settings: ServiceNotificationSettings,
        desktop_notifier: DesktopNotifier | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self.settings = settings
        self.output_dir = output_dir
        self.clock = clock
        self.timezone = ZoneInfo(settings.timezone)
        self.desktop = desktop_notifier or WindowsToastNotifier()
        self.repository = NotificationDeliveryRepository(
            database,
            retry_limit=settings.retry_limit,
        )
        self.query = NotificationQueryService(database, action_queue_path)

    def process(self, result: ServiceRunResult) -> NotificationBatchReport:
        """Process one service result without allowing alert errors to fail the workflow."""

        if not self.settings.enabled:
            return NotificationBatchReport()
        now = self.clock().astimezone(self.timezone)
        priority_alerts = 0
        deadline_alerts = 0
        failure_alerts = 0
        duplicate_events = 0
        summary_path = None
        errors: list[str] = []

        if self.settings.desktop_enabled:
            try:
                if result.outcome is ServiceRunOutcome.SUCCEEDED:
                    delivered, duplicates, delivery_errors = self._priority_alerts(now)
                    priority_alerts += delivered
                    duplicate_events += duplicates
                    errors.extend(delivery_errors)
                    delivered, duplicates, delivery_errors = self._deadline_alerts(now)
                    deadline_alerts += delivered
                    duplicate_events += duplicates
                    errors.extend(delivery_errors)
                else:
                    delivered, duplicates, delivery_errors = self._failure_alert(result, now)
                    failure_alerts += delivered
                    duplicate_events += duplicates
                    errors.extend(delivery_errors)
            except Exception as error:  # noqa: BLE001 - alerts must never fail the workflow
                errors.append(f"{type(error).__name__}: alert processing failed")

        if self.settings.daily_summary_enabled and now.hour >= self.settings.daily_summary_hour:
            try:
                path, duplicate = self._daily_summary(now)
                summary_path = path
                duplicate_events += int(duplicate)
            except Exception as error:  # noqa: BLE001 - summary failures remain isolated
                errors.append(f"{type(error).__name__}: daily summary generation failed")

        return NotificationBatchReport(
            priority_alerts=priority_alerts,
            deadline_alerts=deadline_alerts,
            failure_alerts=failure_alerts,
            duplicate_events=duplicate_events,
            summary_path=summary_path,
            errors=tuple(errors[:20]),
        )

    def _claim_items(
        self,
        items: tuple[DigestItem, ...],
        *,
        kind: NotificationKind,
        now: datetime,
        extra: Callable[[DigestItem], str],
    ) -> tuple[tuple[str, ...], int]:
        claimed: list[str] = []
        duplicates = 0
        for item in items:
            related_hash = _digest("message", item.message_key)
            key = _digest(kind.value, related_hash, item.analysis_fingerprint, extra(item))
            if self.repository.claim(
                dedupe_key=key,
                kind=kind,
                attempted_at=now,
                related_hash=related_hash,
            ):
                claimed.append(key)
            else:
                duplicates += 1
        return tuple(claimed), duplicates

    def _deliver(
        self,
        keys: tuple[str, ...],
        *,
        title: str,
        message: str,
        now: datetime,
    ) -> tuple[int, tuple[str, ...]]:
        if not keys:
            return 0, ()
        try:
            self.desktop.show(title, message)
        except Exception as error:  # noqa: BLE001 - store only a bounded safe error class
            safe_error = f"{type(error).__name__}: desktop delivery failed"
            self.repository.mark_failed(keys, failed_at=now, error_summary=safe_error)
            return 0, (safe_error,)
        self.repository.mark_delivered(keys, now)
        return len(keys), ()

    def _priority_alerts(self, now: datetime) -> tuple[int, int, tuple[str, ...]]:
        delivered = 0
        duplicates = 0
        errors: list[str] = []
        items = self.query.priority_alert_items()
        for priority in (Priority.P1, Priority.P2):
            matching = tuple(item for item in items if item.priority is priority)
            keys, repeated = self._claim_items(
                matching,
                kind=NotificationKind.PRIORITY_ALERT,
                now=now,
                extra=lambda item: item.priority.value,
            )
            duplicates += repeated
            count, delivery_errors = self._deliver(
                keys,
                title=f"InboxPilot · 新增 {priority.value} 邮件",
                message=f"检测到 {len(keys)} 封需要优先处理的邮件，请打开本地控制台查看详情。",
                now=now,
            )
            delivered += count
            errors.extend(delivery_errors)
        return delivered, duplicates, tuple(errors)

    def _deadline_alerts(self, now: datetime) -> tuple[int, int, tuple[str, ...]]:
        items = self.query.deadline_alert_items(
            now=now,
            window_hours=self.settings.deadline_window_hours,
        )
        keys, duplicates = self._claim_items(
            items,
            kind=NotificationKind.DEADLINE_ALERT,
            now=now,
            extra=lambda item: item.deadline.isoformat() if item.deadline is not None else "none",
        )
        nearest = min((item.deadline for item in items if item.deadline is not None), default=None)
        nearest_text = nearest.astimezone(self.timezone).strftime("%m-%d %H:%M") if nearest else ""
        count, errors = self._deliver(
            keys,
            title="InboxPilot · 即将到期事项",
            message=(
                f"检测到 {len(keys)} 个即将到期事项，最近截止时间为 {nearest_text}。"
                "请打开本地控制台查看详情。"
            ),
            now=now,
        )
        return count, duplicates, errors

    def _failure_alert(
        self,
        result: ServiceRunResult,
        now: datetime,
    ) -> tuple[int, int, tuple[str, ...]]:
        failure_class = result.error_type or result.outcome.value
        key = _digest(
            NotificationKind.WORKFLOW_FAILURE.value,
            now.date().isoformat(),
            failure_class,
        )
        claimed = self.repository.claim(
            dedupe_key=key,
            kind=NotificationKind.WORKFLOW_FAILURE,
            attempted_at=now,
            related_hash=_digest("workflow", failure_class),
        )
        if not claimed:
            return 0, 1, ()
        count, errors = self._deliver(
            (key,),
            title="InboxPilot · 工作流需要关注",
            message="本次同步或分析未完全成功，服务已进入安全退避。请查看运行状态。",
            now=now,
        )
        return count, 0, errors

    def _daily_summary(self, now: datetime) -> tuple[Path | None, bool]:
        key = _digest(NotificationKind.DAILY_SUMMARY.value, now.date().isoformat())
        claimed = self.repository.claim(
            dedupe_key=key,
            kind=NotificationKind.DAILY_SUMMARY,
            attempted_at=now,
            related_hash=_digest("summary-date", now.date().isoformat()),
        )
        if not claimed:
            return None, True
        try:
            digest = self.query.daily_digest(
                now=now,
                lookback_hours=self.settings.summary_lookback_hours,
                deadline_window_hours=self.settings.deadline_window_hours,
            )
            path = write_daily_digest(digest, self.output_dir)
        except Exception as error:
            safe_error = f"{type(error).__name__}: summary write failed"
            self.repository.mark_failed((key,), failed_at=now, error_summary=safe_error)
            raise
        self.repository.mark_delivered((key,), now)
        return path, False
