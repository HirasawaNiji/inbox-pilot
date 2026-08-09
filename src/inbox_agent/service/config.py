"""Validated YAML configuration for the local InboxPilot scheduler."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator
from yaml import YAMLError

from inbox_agent.models import FrozenModel
from inbox_agent.workflow import WorkflowRuntimeSettings


class ServiceConfigurationError(Exception):
    """Base class for local scheduler configuration failures."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class ServiceWorkflowSettings(FrozenModel):
    """Workflow paths and switches reused by every scheduled run."""

    dataset_path: Path = Path("data/private/outlook_inbox.json")
    database_path: Path = Path("data/private/inbox_pilot.sqlite3")
    action_queue_path: Path = Path("data/private/action_queue.json")
    audit_log_path: Path = Path("data/private/audit/actions.jsonl")
    policy_path: Path = Path("config/rules.yaml")
    llm_config_path: Path | None = None
    llm_routing_path: Path = Path("config/llm_routing.yaml")
    llm_fusion_path: Path = Path("config/llm_fusion.yaml")
    sync_outlook: bool = False
    graph_config_path: Path = Path("config/graph.local.yaml")

    def runtime_settings(self, project_root: Path) -> WorkflowRuntimeSettings:
        return WorkflowRuntimeSettings(
            project_root=project_root,
            dataset_path=self.dataset_path,
            database_path=self.database_path,
            action_queue_path=self.action_queue_path,
            audit_log_path=self.audit_log_path,
            policy_path=self.policy_path,
            llm_config_path=self.llm_config_path,
            llm_routing_path=self.llm_routing_path,
            llm_fusion_path=self.llm_fusion_path,
            sync_outlook=self.sync_outlook,
            graph_config_path=self.graph_config_path,
        ).resolved()


class ServiceNotificationSettings(FrozenModel):
    """Local alert, deadline reminder, and private digest policy."""

    enabled: bool = True
    desktop_enabled: bool = True
    daily_summary_enabled: bool = True
    output_dir: Path = Path("data/private/summaries")
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=255)
    daily_summary_hour: int = Field(default=18, ge=0, le=23)
    deadline_window_hours: int = Field(default=48, ge=1, le=24 * 30)
    summary_lookback_hours: int = Field(default=24, ge=1, le=24 * 30)
    retry_limit: int = Field(default=3, ge=1, le=10)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError(f"unknown notification timezone: {value}") from error
        return value

    def resolved_output_dir(self, project_root: Path) -> Path:
        if self.output_dir.is_absolute():
            return self.output_dir.resolve()
        return (project_root / self.output_dir).resolve()


class ServiceSettings(FrozenModel):
    """Safe single-process scheduling and retry settings."""

    schema_version: Literal["1.0"] = "1.0"
    service_name: str = Field(
        default="inbox-pilot",
        pattern=r"^[a-z][a-z0-9_-]*$",
        max_length=64,
    )
    interval_minutes: int = Field(default=15, ge=1, le=1_440)
    max_backoff_minutes: int = Field(default=60, ge=1, le=10_080)
    run_immediately: bool = True
    lock_path: Path = Path("data/private/inbox_pilot.service.lock")
    workflow: ServiceWorkflowSettings = ServiceWorkflowSettings()
    notifications: ServiceNotificationSettings = ServiceNotificationSettings()

    @model_validator(mode="after")
    def validate_backoff(self) -> Self:
        if self.max_backoff_minutes < self.interval_minutes:
            raise ValueError("max_backoff_minutes must not be less than interval_minutes")
        return self

    def resolved_lock_path(self, project_root: Path) -> Path:
        if self.lock_path.is_absolute():
            return self.lock_path.resolve()
        return (project_root / self.lock_path).resolve()


def load_service_settings(path: Path) -> ServiceSettings:
    """Read a UTF-8 service YAML without resolving private paths against cwd."""

    try:
        raw_content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ServiceConfigurationError(path, "Unable to read service configuration") from error
    try:
        payload = yaml.safe_load(raw_content)
    except YAMLError as error:
        raise ServiceConfigurationError(path, "Service configuration is invalid YAML") from error
    try:
        return ServiceSettings.model_validate(payload)
    except ValidationError as error:
        raise ServiceConfigurationError(
            path,
            "Service configuration does not match the InboxPilot schema",
        ) from error
