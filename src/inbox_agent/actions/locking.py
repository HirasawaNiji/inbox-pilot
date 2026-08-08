"""Small cross-platform advisory file lock for private Stage 3 state."""

from __future__ import annotations

import importlib
import os
import time
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self


class ActionFileLockError(Exception):
    """Base class for local action-state locking failures."""


class ActionFileLockTimeoutError(ActionFileLockError):
    """Raised when another process holds a private state lock too long."""


class ActionFileLock:
    """Hold one-byte OS advisory lock until the context exits."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        if timeout_seconds < 0:
            raise ValueError("lock timeout must not be negative")
        if poll_interval_seconds <= 0:
            raise ValueError("lock poll interval must be positive")
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._handle: BinaryIO | None = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def acquire(self) -> None:
        """Acquire the lock without waiting beyond the bounded timeout."""

        if self._handle is not None:
            raise ActionFileLockError(f"Lock is already held by this object: {self.path}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ActionFileLockError(
                f"Unable to create action lock directory: {self.path.parent}"
            ) from error

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            handle: BinaryIO | None = None
            try:
                # Creating the first lock byte is itself subject to a Windows
                # sharing race. Keep open, initialization, and OS locking in
                # the same bounded retry loop.
                handle = self.path.open("a+b")
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                self._acquire_handle(handle)
            except OSError as error:
                if handle is not None:
                    handle.close()
                if time.monotonic() >= deadline:
                    raise ActionFileLockTimeoutError(
                        f"Timed out waiting for action lock: {self.path}"
                    ) from error
                time.sleep(self.poll_interval_seconds)
            else:
                self._handle = handle
                return

    def release(self) -> None:
        """Release the advisory lock and close its file handle."""

        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            self._release_handle(handle)
        except OSError as error:
            raise ActionFileLockError(f"Unable to release action lock: {self.path}") from error
        finally:
            handle.close()

    @staticmethod
    def _acquire_handle(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        fcntl = importlib.import_module("fcntl")

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _release_handle(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        fcntl = importlib.import_module("fcntl")

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
