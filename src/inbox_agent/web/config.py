"""Validated local paths used by the Web API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Self

from pydantic import Field

from inbox_agent.models import FrozenModel


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


class WebSettings(FrozenModel):
    """Runtime configuration for one loopback-only API process."""

    project_root: Path = Field(default_factory=_project_root)
    database_path: Path = Path("data/private/inbox_pilot.sqlite3")
    action_queue_path: Path = Path("data/private/action_queue.json")
    audit_log_path: Path = Path("data/private/action_audit.jsonl")
    graph_write_config_path: Path = Path("config/graph_write.local.yaml")
    service_config_path: Path = Path("config/service.local.yaml")
    service_name: str = Field(default="inbox-pilot", min_length=1, max_length=64)

    def resolved(self) -> Self:
        """Return an immutable copy with every runtime path made absolute."""

        root = self.project_root.resolve()

        def resolved(path: Path) -> Path:
            return path.resolve() if path.is_absolute() else (root / path).resolve()

        return self.model_copy(
            update={
                "project_root": root,
                "database_path": resolved(self.database_path),
                "action_queue_path": resolved(self.action_queue_path),
                "audit_log_path": resolved(self.audit_log_path),
                "graph_write_config_path": resolved(self.graph_write_config_path),
                "service_config_path": resolved(self.service_config_path),
            }
        )

    @classmethod
    def from_environment(cls) -> WebSettings:
        """Load optional path overrides without reading secret file contents."""

        values: dict[str, object] = {}
        mapping = {
            "project_root": "INBOX_PILOT_PROJECT_ROOT",
            "database_path": "INBOX_PILOT_DATABASE_PATH",
            "action_queue_path": "INBOX_PILOT_ACTION_QUEUE_PATH",
            "audit_log_path": "INBOX_PILOT_AUDIT_LOG_PATH",
            "graph_write_config_path": "INBOX_PILOT_GRAPH_WRITE_CONFIG_PATH",
            "service_config_path": "INBOX_PILOT_SERVICE_CONFIG_PATH",
            "service_name": "INBOX_PILOT_SERVICE_NAME",
        }
        for field_name, environment_name in mapping.items():
            value = os.getenv(environment_name)
            if value:
                values[field_name] = value
        return cls.model_validate(values)
