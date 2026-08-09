"""Immutable action models and state transitions for controlled mailbox writeback."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from inbox_agent.models import (
    DecisionSource,
    FrozenModel,
    LLMAnalysisResult,
    RuleEvaluation,
    TriageResult,
)

MANAGED_CATEGORY_PREFIX = "InboxPilot/"


class MailboxActionType(StrEnum):
    """Mailbox mutations supported by the Stage 3 safety boundary."""

    SET_CATEGORIES = "set_categories"


class MailboxActionStatus(StrEnum):
    """Lifecycle states for a human-controlled mailbox action."""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    WRITE_IN_FLIGHT = "write_in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    ROLLBACK_EXECUTING = "rollback_executing"
    ROLLBACK_WRITE_IN_FLIGHT = "rollback_write_in_flight"
    ROLLBACK_FAILED = "rollback_failed"
    ROLLBACK_OUTCOME_UNKNOWN = "rollback_outcome_unknown"
    ROLLED_BACK = "rolled_back"


class ActionActor(StrEnum):
    """Actor classes allowed to advance an action state."""

    USER = "user"
    SYSTEM = "system"


def build_action_idempotency_key(
    *,
    message_id: str,
    action_type: MailboxActionType,
    current_categories: tuple[str, ...],
    change_key: str | None,
    managed_categories: tuple[str, ...],
    policy_version: str,
) -> str:
    """Build a stable key from every input that defines one mailbox mutation."""

    payload = {
        "message_id": message_id,
        "action_type": action_type.value,
        "current_categories": tuple(sorted(current_categories, key=str.casefold)),
        "change_key": change_key,
        "managed_categories": tuple(sorted(managed_categories, key=str.casefold)),
        "policy_version": policy_version,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


_ALLOWED_TRANSITIONS: dict[MailboxActionStatus, frozenset[MailboxActionStatus]] = {
    MailboxActionStatus.PENDING_REVIEW: frozenset(
        {MailboxActionStatus.APPROVED, MailboxActionStatus.REJECTED}
    ),
    MailboxActionStatus.APPROVED: frozenset(
        {MailboxActionStatus.EXECUTING, MailboxActionStatus.REJECTED}
    ),
    MailboxActionStatus.REJECTED: frozenset(),
    MailboxActionStatus.EXECUTING: frozenset(
        {
            MailboxActionStatus.SUCCEEDED,
            MailboxActionStatus.FAILED,
            MailboxActionStatus.WRITE_IN_FLIGHT,
        }
    ),
    MailboxActionStatus.WRITE_IN_FLIGHT: frozenset(
        {
            MailboxActionStatus.SUCCEEDED,
            MailboxActionStatus.FAILED,
            MailboxActionStatus.OUTCOME_UNKNOWN,
        }
    ),
    MailboxActionStatus.SUCCEEDED: frozenset({MailboxActionStatus.ROLLBACK_EXECUTING}),
    MailboxActionStatus.FAILED: frozenset(
        {MailboxActionStatus.EXECUTING, MailboxActionStatus.REJECTED}
    ),
    MailboxActionStatus.OUTCOME_UNKNOWN: frozenset(
        {MailboxActionStatus.SUCCEEDED, MailboxActionStatus.FAILED}
    ),
    MailboxActionStatus.ROLLBACK_EXECUTING: frozenset(
        {
            MailboxActionStatus.ROLLED_BACK,
            MailboxActionStatus.ROLLBACK_FAILED,
            MailboxActionStatus.ROLLBACK_WRITE_IN_FLIGHT,
        }
    ),
    MailboxActionStatus.ROLLBACK_WRITE_IN_FLIGHT: frozenset(
        {
            MailboxActionStatus.ROLLED_BACK,
            MailboxActionStatus.ROLLBACK_FAILED,
            MailboxActionStatus.ROLLBACK_OUTCOME_UNKNOWN,
        }
    ),
    MailboxActionStatus.ROLLBACK_FAILED: frozenset({MailboxActionStatus.ROLLBACK_EXECUTING}),
    MailboxActionStatus.ROLLBACK_OUTCOME_UNKNOWN: frozenset(
        {MailboxActionStatus.ROLLED_BACK, MailboxActionStatus.ROLLBACK_FAILED}
    ),
    MailboxActionStatus.ROLLED_BACK: frozenset(),
}

_USER_CONTROLLED_TARGETS = frozenset(
    {
        MailboxActionStatus.APPROVED,
        MailboxActionStatus.REJECTED,
    }
)


def can_transition(
    from_status: MailboxActionStatus,
    to_status: MailboxActionStatus,
) -> bool:
    """Return whether the state machine permits a status transition."""

    return to_status in _ALLOWED_TRANSITIONS[from_status]


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value


def _normalize_categories(categories: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(category.strip() for category in categories)
    if any(not category for category in normalized):
        raise ValueError("category names must not be empty")
    if any(len(category) > 255 for category in normalized):
        raise ValueError("category names must not exceed 255 characters")

    casefolded = [category.casefold() for category in normalized]
    if len(casefolded) != len(set(casefolded)):
        raise ValueError("category names must be unique ignoring case")
    return normalized


class OutlookCategorySnapshot(FrozenModel):
    """Observed Outlook categories before an action is proposed."""

    categories: tuple[str, ...] = Field(default=(), max_length=100)
    observed_at: datetime
    change_key: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, categories: tuple[str, ...]) -> tuple[str, ...]:
        """Normalize category names and reject ambiguous duplicates."""

        return _normalize_categories(categories)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        """Require an absolute timestamp for stale-state checks."""

        return _require_aware(value, "category snapshot timestamp")


class CategoryWritePlan(FrozenModel):
    """Desired InboxPilot-managed categories for one Outlook message."""

    managed_categories: tuple[str, ...] = Field(min_length=2, max_length=3)
    preserve_unmanaged_categories: Literal[True] = True

    @field_validator("managed_categories")
    @classmethod
    def validate_managed_categories(cls, categories: tuple[str, ...]) -> tuple[str, ...]:
        """Restrict planned changes to the InboxPilot namespace."""

        normalized = _normalize_categories(categories)
        if any(not category.startswith(MANAGED_CATEGORY_PREFIX) for category in normalized):
            raise ValueError("managed categories must use the InboxPilot/ prefix")
        return normalized


class RollbackExecutionSnapshot(FrozenModel):
    """Live states persisted immediately before a controlled rollback PATCH."""

    rollback_idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=1_000)
    observed_categories: tuple[str, ...] = Field(max_length=103)
    target_categories: tuple[str, ...] = Field(max_length=103)
    observed_change_key: str = Field(min_length=1, max_length=512)
    observed_at: datetime

    @field_validator("observed_categories", "target_categories")
    @classmethod
    def validate_categories(cls, categories: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_categories(categories)

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return _require_aware(value, "rollback snapshot timestamp")

    @model_validator(mode="after")
    def validate_unmanaged_preservation(self) -> Self:
        """Ensure the rollback changes only the InboxPilot-managed namespace."""

        prefix = MANAGED_CATEGORY_PREFIX.casefold()
        observed = {
            category.casefold()
            for category in self.observed_categories
            if not category.casefold().startswith(prefix)
        }
        target = {
            category.casefold()
            for category in self.target_categories
            if not category.casefold().startswith(prefix)
        }
        if observed != target:
            raise ValueError("rollback snapshot must preserve all live unmanaged categories")
        return self


class ActionEvidence(FrozenModel):
    """Rule, LLM, and final triage evidence supporting an action proposal."""

    rule_evaluation: RuleEvaluation
    llm_analysis: LLMAnalysisResult | None = None
    triage_result: TriageResult

    @model_validator(mode="after")
    def validate_decision_provenance(self) -> Self:
        """Require internally consistent evidence and message identity."""

        if self.llm_analysis is not None:
            if self.llm_analysis.message_id != self.triage_result.message_id:
                raise ValueError("LLM analysis and triage result message IDs must match")

        if self.triage_result.decision_source is not DecisionSource.RULE:
            if self.llm_analysis is None:
                raise ValueError("LLM or hybrid decisions require an LLM analysis")

        if self.triage_result.decision_source is DecisionSource.RULE:
            if self.rule_evaluation.suggested_priority is not self.triage_result.priority:
                raise ValueError("rule decision priority must match the rule evaluation")
        return self


class ActionTransition(FrozenModel):
    """One validated state change suitable for later audit emission."""

    from_status: MailboxActionStatus
    to_status: MailboxActionStatus
    occurred_at: datetime
    actor: ActionActor
    note: str | None = Field(default=None, min_length=1, max_length=1_000)

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        """Require timezone context for every state change."""

        return _require_aware(value, "action transition timestamp")

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        """Enforce the state graph and human-control boundary."""

        if not can_transition(self.from_status, self.to_status):
            raise ValueError(f"invalid action transition: {self.from_status} -> {self.to_status}")

        expected_actor = (
            ActionActor.USER if self.to_status in _USER_CONTROLLED_TARGETS else ActionActor.SYSTEM
        )
        if self.actor is not expected_actor:
            raise ValueError(f"transition to {self.to_status} requires actor={expected_actor}")

        if self.to_status in {
            MailboxActionStatus.FAILED,
            MailboxActionStatus.OUTCOME_UNKNOWN,
            MailboxActionStatus.ROLLBACK_FAILED,
            MailboxActionStatus.ROLLBACK_OUTCOME_UNKNOWN,
            MailboxActionStatus.ROLLED_BACK,
        }:
            if self.note is None:
                raise ValueError(f"transition to {self.to_status} requires a note")
        return self


class MailboxAction(FrozenModel):
    """A reviewable, immutable proposal for one constrained mailbox mutation."""

    schema_version: Literal["1.0"] = "1.0"
    action_id: str = Field(
        pattern=r"^action-[A-Za-z0-9][A-Za-z0-9._-]*$",
        min_length=8,
        max_length=128,
    )
    message_id: str = Field(min_length=1, max_length=512)
    action_type: Literal[MailboxActionType.SET_CATEGORIES] = MailboxActionType.SET_CATEGORIES

    current_snapshot: OutlookCategorySnapshot
    write_plan: CategoryWritePlan
    evidence: ActionEvidence

    status: MailboxActionStatus = MailboxActionStatus.PENDING_REVIEW
    created_at: datetime
    updated_at: datetime
    transition_history: tuple[ActionTransition, ...] = Field(default=(), max_length=100)

    idempotency_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    rollback_snapshot: RollbackExecutionSnapshot | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_action_timestamp(cls, value: datetime) -> datetime:
        """Require timezone-aware lifecycle timestamps."""

        return _require_aware(value, "mailbox action timestamp")

    @model_validator(mode="after")
    def validate_action_contract(self) -> Self:
        """Cross-check identity, plan, evidence, and transition history."""

        if self.message_id != self.evidence.triage_result.message_id:
            raise ValueError("action and triage result message IDs must match")
        if self.current_snapshot.observed_at > self.created_at:
            raise ValueError("category snapshot must not be newer than the action")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")

        expected_categories = {
            f"{MANAGED_CATEGORY_PREFIX}{self.evidence.triage_result.priority.value}",
            f"{MANAGED_CATEGORY_PREFIX}{self.evidence.triage_result.category}",
        }
        if self.evidence.triage_result.requires_review:
            expected_categories.add(f"{MANAGED_CATEGORY_PREFIX}review")
        if set(self.write_plan.managed_categories) != expected_categories:
            raise ValueError("write plan categories must match the final triage result")

        if self.idempotency_key is not None:
            expected_key = build_action_idempotency_key(
                message_id=self.message_id,
                action_type=self.action_type,
                current_categories=self.current_snapshot.categories,
                change_key=self.current_snapshot.change_key,
                managed_categories=self.write_plan.managed_categories,
                policy_version=self.evidence.triage_result.policy_version,
            )
            if self.idempotency_key != expected_key:
                raise ValueError("idempotency_key does not match the mailbox mutation")

        rollback_snapshot_required = {
            MailboxActionStatus.ROLLBACK_WRITE_IN_FLIGHT,
            MailboxActionStatus.ROLLBACK_OUTCOME_UNKNOWN,
            MailboxActionStatus.ROLLED_BACK,
        }
        if self.status in rollback_snapshot_required and self.rollback_snapshot is None:
            raise ValueError("rollback state requires a persisted live snapshot")
        if self.rollback_snapshot is not None:
            prefix = MANAGED_CATEGORY_PREFIX.casefold()
            original_managed = {
                category.casefold()
                for category in self.current_snapshot.categories
                if category.casefold().startswith(prefix)
            }
            target_managed = {
                category.casefold()
                for category in self.rollback_snapshot.target_categories
                if category.casefold().startswith(prefix)
            }
            if target_managed != original_managed:
                raise ValueError("rollback target must restore the original managed categories")

        self._validate_transition_history()
        return self

    def _validate_transition_history(self) -> None:
        if not self.transition_history:
            if self.status is not MailboxActionStatus.PENDING_REVIEW:
                raise ValueError("an action without transition history must be pending_review")
            if self.updated_at != self.created_at:
                raise ValueError("a new action must have matching created_at and updated_at")
            return

        expected_from = MailboxActionStatus.PENDING_REVIEW
        previous_time = self.created_at
        for transition in self.transition_history:
            if transition.from_status is not expected_from:
                raise ValueError("action transition history contains a broken status chain")
            if transition.occurred_at < previous_time:
                raise ValueError("action transition history must be chronological")
            expected_from = transition.to_status
            previous_time = transition.occurred_at

        if self.status is not expected_from:
            raise ValueError("action status must match the final transition")
        if self.updated_at != previous_time:
            raise ValueError("updated_at must match the final transition timestamp")

    def transition(
        self,
        to_status: MailboxActionStatus,
        *,
        occurred_at: datetime,
        actor: ActionActor,
        note: str | None = None,
    ) -> Self:
        """Return a validated copy advanced by one state transition."""

        transition = ActionTransition(
            from_status=self.status,
            to_status=to_status,
            occurred_at=occurred_at,
            actor=actor,
            note=note,
        )
        payload = self.model_dump()
        payload["status"] = to_status
        payload["updated_at"] = occurred_at
        payload["transition_history"] = (*self.transition_history, transition)
        return type(self).model_validate(payload)
