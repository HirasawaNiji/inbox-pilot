"""Local alerts and privacy-conscious daily summaries."""

from inbox_agent.notifications.desktop import (
    DesktopNotificationError,
    DesktopNotifier,
    RecordingDesktopNotifier,
    WindowsToastNotifier,
)
from inbox_agent.notifications.manager import NotificationCoordinator
from inbox_agent.notifications.models import (
    DailyDigest,
    DigestItem,
    NotificationBatchReport,
    NotificationDeliveryStatus,
    NotificationKind,
)
from inbox_agent.notifications.repository import (
    NotificationDeliveryRecord,
    NotificationDeliveryRepository,
)
from inbox_agent.notifications.summary import render_daily_digest, write_daily_digest

__all__ = [
    "DailyDigest",
    "DesktopNotificationError",
    "DesktopNotifier",
    "DigestItem",
    "NotificationBatchReport",
    "NotificationCoordinator",
    "NotificationDeliveryRecord",
    "NotificationDeliveryRepository",
    "NotificationDeliveryStatus",
    "NotificationKind",
    "RecordingDesktopNotifier",
    "WindowsToastNotifier",
    "render_daily_digest",
    "write_daily_digest",
]
