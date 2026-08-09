"""Thread-safe shutdown capability for a managed local Web server."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock


class WebShutdownController:
    """Expose a narrow, one-shot graceful-shutdown callback to the Web app."""

    def __init__(self) -> None:
        self._callback: Callable[[], None] | None = None
        self._requested = False
        self._lock = Lock()

    @property
    def available(self) -> bool:
        """Return whether this app was launched by the managed server."""

        with self._lock:
            return self._callback is not None

    @property
    def requested(self) -> bool:
        """Return whether a graceful shutdown was already requested."""

        with self._lock:
            return self._requested

    def bind(self, callback: Callable[[], None]) -> None:
        """Bind the server-owned callback exactly once."""

        with self._lock:
            if self._callback is not None:
                raise RuntimeError("The Web shutdown callback is already bound")
            self._callback = callback

    def request_shutdown(self) -> bool:
        """Request shutdown once, returning false when no managed server exists."""

        with self._lock:
            callback = self._callback
            if callback is None:
                return False
            if self._requested:
                return True
            self._requested = True
        callback()
        return True
