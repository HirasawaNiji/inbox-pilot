"""Structured JSONL logging with central secret and private-content redaction."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from inbox_agent.observability.models import ObservabilityEvent

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|subject|body|content|preview)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{12,}\b"),
)


def sanitize_text(value: str, *, maximum_length: int = 500) -> str:
    """Bound text and remove credential-shaped values before persistence."""

    sanitized = value.replace("\r", " ").replace("\n", " ")
    for pattern in _SECRET_VALUE_PATTERNS:
        sanitized = pattern.sub("<redacted>", sanitized)
    return sanitized[:maximum_length]


def sanitize_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return JSON-safe metadata without known private-content fields."""

    sanitized: dict[str, Any] = {}
    for key, value in values.items():
        safe_key = sanitize_text(str(key), maximum_length=64)
        if _SENSITIVE_KEY.search(safe_key):
            sanitized[safe_key] = "<redacted>"
        elif value is None or isinstance(value, (bool, int, float)):
            sanitized[safe_key] = value
        elif isinstance(value, str):
            sanitized[safe_key] = sanitize_text(value)
        else:
            sanitized[safe_key] = sanitize_text(str(value))
    return sanitized


class StructuredLogWriter:
    """Append one privacy-bounded JSON object per line."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.Lock()

    def write(self, event: ObservabilityEvent) -> None:
        payload = event.model_dump(mode="json", exclude_none=True)
        for key, value in tuple(payload.items()):
            if key != "details" and isinstance(value, str):
                payload[key] = sanitize_text(value)
        payload["details"] = sanitize_mapping(event.details)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.write("\n")
