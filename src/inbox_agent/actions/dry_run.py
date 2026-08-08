"""Pure local dry-run planning for approved mailbox category actions."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from inbox_agent.actions.models import (
    MANAGED_CATEGORY_PREFIX,
    MailboxAction,
    MailboxActionStatus,
    MailboxActionType,
)
from inbox_agent.actions.queue import ActionQueue
from inbox_agent.models import FrozenModel


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dry-run timestamp must include timezone information")
    return value


def _category_key(category: str) -> str:
    return category.casefold()


def _is_managed(category: str) -> bool:
    return _category_key(category).startswith(MANAGED_CATEGORY_PREFIX.casefold())


def _unique_categories(categories: tuple[str, ...]) -> tuple[str, ...]:
    keys = [_category_key(category) for category in categories]
    if len(keys) != len(set(keys)):
        raise ValueError("dry-run categories must be unique ignoring case")
    return categories


class ActionDryRunPlan(FrozenModel):
    """Exact category difference for one approved action, without execution."""

    action_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=512)
    action_type: Literal[MailboxActionType.SET_CATEGORIES]
    source_status: Literal[MailboxActionStatus.APPROVED]
    change_key: str | None = Field(default=None, min_length=1, max_length=512)

    current_categories: tuple[str, ...] = Field(max_length=100)
    managed_categories: tuple[str, ...] = Field(min_length=2, max_length=3)
    add_categories: tuple[str, ...] = Field(max_length=3)
    remove_categories: tuple[str, ...] = Field(max_length=100)
    final_categories: tuple[str, ...] = Field(max_length=103)
    preserve_unmanaged_categories: Literal[True] = True
    would_write: bool

    @field_validator(
        "current_categories",
        "managed_categories",
        "add_categories",
        "remove_categories",
        "final_categories",
    )
    @classmethod
    def validate_unique_categories(cls, categories: tuple[str, ...]) -> tuple[str, ...]:
        """Reject ambiguous plans before they are displayed or serialized."""

        return _unique_categories(categories)

    @model_validator(mode="after")
    def validate_category_difference(self) -> Self:
        """Prove that the plan preserves user categories and reports an exact diff."""

        current_keys = {_category_key(category) for category in self.current_categories}
        final_keys = {_category_key(category) for category in self.final_categories}
        managed_keys = {_category_key(category) for category in self.managed_categories}

        if any(not _is_managed(category) for category in self.managed_categories):
            raise ValueError("dry-run managed categories must use the InboxPilot/ prefix")

        unmanaged_current = tuple(
            category for category in self.current_categories if not _is_managed(category)
        )
        if any(_category_key(category) not in final_keys for category in unmanaged_current):
            raise ValueError("dry-run plan must preserve every unmanaged category")
        if not managed_keys <= final_keys:
            raise ValueError("dry-run final categories must contain the managed plan")

        expected_add = tuple(
            category
            for category in self.managed_categories
            if _category_key(category) not in current_keys
        )
        expected_remove = tuple(
            category
            for category in self.current_categories
            if _is_managed(category) and _category_key(category) not in managed_keys
        )
        if self.add_categories != expected_add:
            raise ValueError("dry-run add_categories does not match the category difference")
        if self.remove_categories != expected_remove:
            raise ValueError("dry-run remove_categories does not match the category difference")
        if self.would_write is not bool(expected_add or expected_remove):
            raise ValueError("dry-run would_write does not match the category difference")
        return self


class DryRunReport(FrozenModel):
    """Batch plan proving that no Graph write requests were sent."""

    schema_version: Literal["1.0"] = "1.0"
    generated_at: datetime
    queue_path: Path
    queue_total_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    would_write_count: int = Field(ge=0)
    no_change_count: int = Field(ge=0)
    graph_write_request_count: Literal[0] = 0
    plans: tuple[ActionDryRunPlan, ...] = ()

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        """Require a traceable dry-run time."""

        return _require_aware(value)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        """Keep all published plan totals internally consistent."""

        if self.queue_total_count != self.eligible_count + self.skipped_count:
            raise ValueError("dry-run queue totals are inconsistent")
        if self.eligible_count != len(self.plans):
            raise ValueError("dry-run eligible count must match plans")
        if self.would_write_count != sum(plan.would_write for plan in self.plans):
            raise ValueError("dry-run write count must match plans")
        if self.no_change_count != self.eligible_count - self.would_write_count:
            raise ValueError("dry-run no-change count is inconsistent")
        return self


def _plan_action(action: MailboxAction) -> ActionDryRunPlan:
    if action.status is not MailboxActionStatus.APPROVED:
        raise ValueError("dry-run can plan only approved actions")

    current = action.current_snapshot.categories
    managed = action.write_plan.managed_categories
    current_keys = {_category_key(category) for category in current}
    managed_keys = {_category_key(category) for category in managed}

    unmanaged = tuple(category for category in current if not _is_managed(category))
    additions = tuple(
        category for category in managed if _category_key(category) not in current_keys
    )
    removals = tuple(
        category
        for category in current
        if _is_managed(category) and _category_key(category) not in managed_keys
    )
    final_categories = (*unmanaged, *managed)

    return ActionDryRunPlan(
        action_id=action.action_id,
        message_id=action.message_id,
        action_type=action.action_type,
        source_status=MailboxActionStatus.APPROVED,
        change_key=action.current_snapshot.change_key,
        current_categories=current,
        managed_categories=managed,
        add_categories=additions,
        remove_categories=removals,
        final_categories=final_categories,
        would_write=bool(additions or removals),
    )


def build_dry_run(
    queue: ActionQueue,
    queue_path: str | Path,
    *,
    generated_at: datetime | None = None,
) -> DryRunReport:
    """Build plans for approved actions without importing or calling Graph clients."""

    plans = tuple(
        _plan_action(action)
        for action in queue.actions
        if action.status is MailboxActionStatus.APPROVED
    )
    would_write_count = sum(plan.would_write for plan in plans)
    return DryRunReport(
        generated_at=generated_at or _utc_now(),
        queue_path=Path(queue_path),
        queue_total_count=len(queue.actions),
        eligible_count=len(plans),
        skipped_count=len(queue.actions) - len(plans),
        would_write_count=would_write_count,
        no_change_count=len(plans) - would_write_count,
        plans=plans,
    )
