"""Native local desktop delivery with privacy-safe message content."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from typing import Protocol


class DesktopNotificationError(RuntimeError):
    """Raised when the operating system rejects a local alert."""


class DesktopNotifier(Protocol):
    """Small injectable interface for native notification delivery."""

    def show(self, title: str, message: str) -> None:
        """Display one local alert."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

_WINDOWS_TOAST_SCRIPT = "\n".join(
    (
        "$ErrorActionPreference = 'Stop'",
        (
            "[Windows.UI.Notifications.ToastNotificationManager, "
            "Windows.UI.Notifications, ContentType = WindowsRuntime] > $null"
        ),
        (
            "[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] > $null"
        ),
        (
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
            "ContentType = WindowsRuntime] > $null"
        ),
        "$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02",
        (
            "$xml = [Windows.UI.Notifications.ToastNotificationManager]"
            "::GetTemplateContent($template)"
        ),
        "$nodes = $xml.GetElementsByTagName('text')",
        ("$nodes.Item(0).AppendChild($xml.CreateTextNode($env:INBOX_PILOT_TOAST_TITLE)) > $null"),
        ("$nodes.Item(1).AppendChild($xml.CreateTextNode($env:INBOX_PILOT_TOAST_MESSAGE)) > $null"),
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)",
        (
            "[Windows.UI.Notifications.ToastNotificationManager]"
            "::CreateToastNotifier('InboxPilot').Show($toast)"
        ),
    )
)


class WindowsToastNotifier:
    """Deliver a Windows 10/11 toast without adding a third-party dependency."""

    def __init__(
        self,
        *,
        runner: CommandRunner = subprocess.run,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.runner = runner
        self.environment = dict(environment) if environment is not None else dict(os.environ)

    def show(self, title: str, message: str) -> None:
        if os.name != "nt":
            raise DesktopNotificationError("native desktop notifications require Windows")
        environment = dict(self.environment)
        environment["INBOX_PILOT_TOAST_TITLE"] = title[:80]
        environment["INBOX_PILOT_TOAST_MESSAGE"] = message[:240]
        try:
            result = self.runner(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    _WINDOWS_TOAST_SCRIPT,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DesktopNotificationError("Windows rejected the local toast") from error
        if result.returncode != 0:
            raise DesktopNotificationError("Windows rejected the local toast")


class RecordingDesktopNotifier:
    """In-memory notifier used by deterministic tests and local dry runs."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def show(self, title: str, message: str) -> None:
        self.messages.append((title, message))
