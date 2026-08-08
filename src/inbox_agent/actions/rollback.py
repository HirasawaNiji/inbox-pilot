"""Pure local rollback planning for previously succeeded category actions."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from inbox_agent.actions.models import (
    MANAGED_CATEGORY_PREFIX,
    MailboxActionStatus,
    MailboxActionType,
)
from inbox_agent.actions.queue import ActionQueue
from inbox_agent.models import FrozenModel


class RollbackPlanError(Exception):
    """Raised when an action cannot produce a safe local rollback plan."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("rollback timestamp must include timezone information")
    return value


def _category_key(category: str) -> str:
    return category.casefold()


def _is_managed(category: str) -> bool:
    return _category_key(category).startswith(MANAGED_CATEGORY_PREFIX.casefold())


def _unique_categories(categories: tuple[str, ...]) -> tuple[str, ...]:
    keys = [_category_key(category) for category in categories]
    if len(keys) != len(set(keys)):
        raise ValueError("rollback categories must be unique ignoring case")
    return categories


def build_rollback_idempotency_key(
    *,
    action_id: str,
    forward_idempotency_key: str,
    expected_current_categories: tuple[str, ...],
    final_categories: tuple[str, ...],
) -> str:
    """Build a stable key for the semantic category restoration operation."""

    payload = {
        "action_id": action_id,
        "forward_idempotency_key": forward_idempotency_key,
        "expected_current_categories": tuple(sorted(expected_current_categories, key=str.casefold)),
        "final_categories": tuple(sorted(final_categories, key=str.casefold)),
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class RollbackDryRunPlan(FrozenModel):
    """Exact expected category restoration for one succeeded action."""

    action_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=512)
    action_type: Literal[MailboxActionType.SET_CATEGORIES]
    source_status: Literal[MailboxActionStatus.SUCCEEDED]
    reason: str = Field(min_length=1, max_length=1_000)

    forward_idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    rollback_idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_change_key: str | None = Field(default=None, min_length=1, max_length=512)

    original_categories: tuple[str, ...] = Field(max_length=100)
    forward_managed_categories: tuple[str, ...] = Field(min_length=2, max_length=3)
    expected_current_categories: tuple[str, ...] = Field(max_length=103)
    restore_managed_categories: tuple[str, ...] = Field(max_length=100)
    add_categories: tuple[str, ...] = Field(max_length=100)
    remove_categories: tuple[str, ...] = Field(max_length=3)
    final_categories: tuple[str, ...] = Field(max_length=100)
    preserve_unmanaged_categories: Literal[True] = True
    would_write: bool

    @field_validator(
        "original_categories",
        "forward_managed_categories",
        "expected_current_categories",
        "restore_managed_categories",
        "add_categories",
        "remove_categories",
        "final_categories",
    )
    @classmethod
    def validate_unique_categories(cls, categories: tuple[str, ...]) -> tuple[str, ...]:
        """Reject ambiguous rollback category sets."""

        return _unique_categories(categories)

    @model_validator(mode="after")
    def validate_rollback_difference(self) -> Self:
        """Prove restoration, preservation, exact diff, and idempotency semantics."""

        original_keys = {_category_key(category) for category in self.original_categories}
        expected_keys = {_category_key(category) for category in self.expected_current_categories}
        restore_keys = {_category_key(category) for category in self.restore_managed_categories}
        forward_keys = {_category_key(category) for category in self.forward_managed_categories}
        final_keys = {_category_key(category) for category in self.final_categories}

        if any(not _is_managed(category) for category in self.restore_managed_categories):
            raise ValueError("rollback restore categories must use the InboxPilot/ prefix")
        if any(not _is_managed(category) for category in self.forward_managed_categories):
            raise ValueError("rollback forward categories must use the InboxPilot/ prefix")
        if restore_keys != {
            _category_key(category)
            for category in self.original_categories
            if _is_managed(category)
        }:
            raise ValueError("rollback restore categories must match the original snapshot")
        if final_keys != original_keys:
            raise ValueError("rollback final categories must restore the original snapshot")

        unmanaged_original = tuple(
            category for category in self.original_categories if not _is_managed(category)
        )
        unmanaged_expected = tuple(
            category for category in self.expected_current_categories if not _is_managed(category)
        )
        if {_category_key(category) for category in unmanaged_expected} != {
            _category_key(category) for category in unmanaged_original
        }:
            raise ValueError(
                "rollback expected state must contain exactly the original user categories"
            )
        if {
            _category_key(category)
            for category in self.expected_current_categories
            if _is_managed(category)
        } != forward_keys:
            raise ValueError("rollback expected state must contain the forward managed categories")
        if any(_category_key(category) not in expected_keys for category in unmanaged_original):
            raise ValueError("rollback expected state must preserve original user categories")
        if any(_category_key(category) not in final_keys for category in unmanaged_original):
            raise ValueError("rollback final state must preserve original user categories")

        expected_add = tuple(
            category
            for category in self.restore_managed_categories
            if _category_key(category) not in expected_keys
        )
        expected_remove = tuple(
            category
            for category in self.expected_current_categories
            if _is_managed(category) and _category_key(category) not in restore_keys
        )
        if self.add_categories != expected_add:
            raise ValueError("rollback add_categories does not match the category difference")
        if self.remove_categories != expected_remove:
            raise ValueError("rollback remove_categories does not match the category difference")
        if self.would_write is not bool(expected_add or expected_remove):
            raise ValueError("rollback would_write does not match the category difference")

        expected_key = build_rollback_idempotency_key(
            action_id=self.action_id,
            forward_idempotency_key=self.forward_idempotency_key,
            expected_current_categories=self.expected_current_categories,
            final_categories=self.final_categories,
        )
        if self.rollback_idempotency_key != expected_key:
            raise ValueError("rollback idempotency key does not match the restoration")
        return self


class RollbackDryRunReport(FrozenModel):
    """One explicitly requested rollback preview with zero Graph writes."""

    schema_version: Literal["1.0"] = "1.0"
    generated_at: datetime
    queue_path: Path
    graph_write_request_count: Literal[0] = 0
    plan: RollbackDryRunPlan

    @field_validator("generated_at")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        """Require a traceable rollback preview time."""

        return _require_aware(value)


def build_rollback_dry_run(
    queue: ActionQueue,
    action_id: str,
    queue_path: str | Path,
    *,
    reason: str,
    generated_at: datetime | None = None,
) -> RollbackDryRunReport:
    """Build a rollback preview for exactly one succeeded action."""

    action = queue.find(action_id)
    if action is None:
        raise RollbackPlanError(f"Action does not exist: {action_id}")
    if action.status is not MailboxActionStatus.SUCCEEDED:
        raise RollbackPlanError(
            f"Rollback dry-run requires a succeeded action, got {action.status}: {action_id}"
        )
    if action.idempotency_key is None:
        raise RollbackPlanError(f"Succeeded action has no idempotency key: {action_id}")

    normalized_reason = reason.strip()
    if not normalized_reason:
        raise RollbackPlanError("Rollback reason must not be empty")

    original = action.current_snapshot.categories
    original_managed = tuple(category for category in original if _is_managed(category))
    original_unmanaged = tuple(category for category in original if not _is_managed(category))
    expected_current = (*original_unmanaged, *action.write_plan.managed_categories)
    expected_keys = {_category_key(category) for category in expected_current}
    original_managed_keys = {_category_key(category) for category in original_managed}
    additions = tuple(
        category for category in original_managed if _category_key(category) not in expected_keys
    )
    removals = tuple(
        category
        for category in expected_current
        if _is_managed(category) and _category_key(category) not in original_managed_keys
    )
    rollback_key = build_rollback_idempotency_key(
        action_id=action.action_id,
        forward_idempotency_key=action.idempotency_key,
        expected_current_categories=expected_current,
        final_categories=original,
    )
    plan = RollbackDryRunPlan(
        action_id=action.action_id,
        message_id=action.message_id,
        action_type=action.action_type,
        source_status=MailboxActionStatus.SUCCEEDED,
        reason=normalized_reason,
        forward_idempotency_key=action.idempotency_key,
        rollback_idempotency_key=rollback_key,
        original_change_key=action.current_snapshot.change_key,
        original_categories=original,
        forward_managed_categories=action.write_plan.managed_categories,
        expected_current_categories=expected_current,
        restore_managed_categories=original_managed,
        add_categories=additions,
        remove_categories=removals,
        final_categories=original,
        would_write=bool(additions or removals),
    )
    return RollbackDryRunReport(
        generated_at=generated_at or _utc_now(),
        queue_path=Path(queue_path),
        plan=plan,
    )
