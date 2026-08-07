"""YAML-driven, deterministic, and explainable mail-priority rules."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator
from yaml import YAMLError

from inbox_agent.models import (
    FrozenModel,
    Importance,
    MessageFeatures,
    NormalizedMessage,
    Priority,
    RuleEvaluation,
    ScoreReason,
    UserContext,
)

_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*(?:年|[-/])\s*"
    r"(?P<month>\d{1,2})\s*(?:月|[-/])\s*"
    r"(?P<day>\d{1,2})\s*日?"
    r"(?:\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}))?"
)
_CLAUSE_BOUNDARY_PATTERN = re.compile(r"[。！？；;\n]+")


class KeywordPolicy(FrozenModel):
    """Keyword groups used to derive explainable message signals."""

    urgent: tuple[str, ...] = ()
    security: tuple[str, ...] = ()
    bulk: tuple[str, ...] = ()
    deadline: tuple[str, ...] = ()
    action: tuple[str, ...] = ()
    opportunity: tuple[str, ...] = ()
    no_action: tuple[str, ...] = ()
    unsubscribe: tuple[str, ...] = ()

    @field_validator("*")
    @classmethod
    def normalize_keywords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Normalize keyword case and reject empty or duplicated entries."""

        normalized = tuple(value.lower().strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("keywords must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("keywords in one group must be unique")
        return normalized


class RuleWeights(FrozenModel):
    """Score changes for every supported rule signal."""

    trusted_sender: int = Field(ge=-100, le=100)
    trusted_domain: int = Field(ge=-100, le=100)
    direct_recipient: int = Field(ge=-100, le=100)
    urgent_keyword: int = Field(ge=-100, le=100)
    security_keyword: int = Field(ge=-100, le=100)
    deadline_within_two_days: int = Field(ge=-100, le=100)
    deadline_within_seven_days: int = Field(ge=-100, le=100)
    deadline_later: int = Field(ge=-100, le=100)
    deadline_without_date: int = Field(ge=-100, le=100)
    action_keyword: int = Field(ge=-100, le=100)
    opportunity_keyword: int = Field(ge=-100, le=100)
    high_importance: int = Field(ge=-100, le=100)
    low_importance: int = Field(ge=-100, le=100)
    has_attachment: int = Field(ge=-100, le=100)
    bulk_mail: int = Field(ge=-100, le=100)
    bulk_keyword: int = Field(ge=-100, le=100)
    unsubscribe: int = Field(ge=-100, le=100)
    no_action_required: int = Field(ge=-100, le=100)
    external_sender: int = Field(ge=-100, le=100)
    empty_subject: int = Field(ge=-100, le=100)
    sender_mismatch: int = Field(ge=-100, le=100)


class PriorityThresholds(FrozenModel):
    """Minimum inclusive scores for the four non-default priorities."""

    p1: int = Field(alias="P1", ge=0, le=100)
    p2: int = Field(alias="P2", ge=0, le=100)
    p3: int = Field(alias="P3", ge=0, le=100)
    p4: int = Field(alias="P4", ge=0, le=100)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        """Require strictly descending thresholds from P1 to P4."""

        if not self.p1 > self.p2 > self.p3 > self.p4:
            raise ValueError("priority thresholds must satisfy P1 > P2 > P3 > P4")
        return self


class RulePolicy(FrozenModel):
    """Complete validated policy loaded from YAML."""

    policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=100)
    base_score: int = Field(ge=0, le=100)
    user_context: UserContext
    bulk_recipient_prefixes: tuple[str, ...] = ()
    bulk_recipient_threshold: int = Field(default=10, ge=1)
    keywords: KeywordPolicy
    weights: RuleWeights
    thresholds: PriorityThresholds
    review_margin: int = Field(default=0, ge=0, le=20)

    @field_validator("bulk_recipient_prefixes")
    @classmethod
    def normalize_bulk_prefixes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Normalize configured local-part prefixes."""

        normalized = tuple(value.lower().strip() for value in values)
        if any(not value or "@" in value for value in normalized):
            raise ValueError("bulk recipient prefixes must be non-empty local-part prefixes")
        return normalized


class RulePolicyError(Exception):
    """Base class for policy loading failures."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class RulePolicyNotFoundError(RulePolicyError):
    """Raised when a policy file is missing."""


class RulePolicyReadError(RulePolicyError):
    """Raised when a policy file cannot be read."""


class RulePolicyYAMLError(RulePolicyError):
    """Raised when a policy file contains invalid YAML."""


class RulePolicyValidationError(RulePolicyError):
    """Raised when YAML data does not match the policy schema."""

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(path, "Rule policy does not match the InboxPilot schema")


def load_policy(path: str | Path) -> RulePolicy:
    """Read and validate one UTF-8 YAML rule policy."""

    policy_path = Path(path)
    try:
        raw_content = policy_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise RulePolicyNotFoundError(policy_path, "Rule policy file does not exist") from error
    except OSError as error:
        raise RulePolicyReadError(policy_path, "Unable to read rule policy file") from error

    try:
        payload = yaml.safe_load(raw_content)
    except YAMLError as error:
        raise RulePolicyYAMLError(policy_path, "Rule policy contains invalid YAML") from error

    try:
        return RulePolicy.model_validate(payload)
    except ValidationError as error:
        raise RulePolicyValidationError(policy_path, error) from error


def _match_keywords(text: str, keywords: tuple[str, ...]) -> tuple[str, ...]:
    """Return configured keywords present in case-normalized text."""

    lowered_text = text.lower()
    return tuple(keyword for keyword in keywords if keyword in lowered_text)


def _is_bulk_recipient(address: str, prefixes: tuple[str, ...]) -> bool:
    """Check whether a recipient local part represents a known mailing list."""

    local_part = address.partition("@")[0]
    return any(local_part.startswith(prefix) for prefix in prefixes)


def _extract_deadline_dates(
    text: str,
    deadline_keywords: tuple[str, ...],
    reference: datetime,
) -> tuple[datetime, ...]:
    """Extract dates only from clauses that contain deadline language."""

    detected: set[datetime] = set()
    for clause in _CLAUSE_BOUNDARY_PATTERN.split(text):
        lowered_clause = clause.lower()
        if not any(keyword in lowered_clause for keyword in deadline_keywords):
            continue
        for match in _DATE_PATTERN.finditer(clause):
            try:
                detected.add(
                    datetime(
                        year=int(match.group("year")),
                        month=int(match.group("month")),
                        day=int(match.group("day")),
                        hour=int(match.group("hour") or 23),
                        minute=int(match.group("minute") or 59),
                        tzinfo=reference.tzinfo,
                    )
                )
            except ValueError:
                continue
    return tuple(sorted(detected))


def extract_features(message: NormalizedMessage, policy: RulePolicy) -> MessageFeatures:
    """Derive deterministic policy signals from a normalized message."""

    searchable_text = "\n".join((message.subject, message.body_text, message.body_preview))
    no_action_keywords = _match_keywords(searchable_text, policy.keywords.no_action)
    action_keywords = tuple(
        keyword
        for keyword in _match_keywords(searchable_text, policy.keywords.action)
        if not any(keyword in phrase for phrase in no_action_keywords)
    )
    trusted_sender = message.from_address in policy.user_context.trusted_senders
    trusted_domain = message.from_domain in policy.user_context.trusted_domains
    directly_addressed = bool(set(message.to_addresses) & policy.user_context.mailbox_addresses)
    recipient_addresses = message.to_addresses + message.cc_addresses
    looks_like_bulk = message.recipient_count >= policy.bulk_recipient_threshold or any(
        _is_bulk_recipient(address, policy.bulk_recipient_prefixes)
        for address in recipient_addresses
    )

    return MessageFeatures(
        sender_is_trusted=trusted_sender,
        sender_domain_is_trusted=trusted_domain,
        directly_addressed=directly_addressed,
        recipient_count=message.recipient_count,
        looks_like_bulk_mail=looks_like_bulk,
        urgent_keywords=_match_keywords(searchable_text, policy.keywords.urgent),
        security_keywords=_match_keywords(searchable_text, policy.keywords.security),
        bulk_keywords=_match_keywords(searchable_text, policy.keywords.bulk),
        action_keywords=action_keywords,
        opportunity_keywords=_match_keywords(searchable_text, policy.keywords.opportunity),
        no_action_keywords=no_action_keywords,
        contains_unsubscribe=bool(_match_keywords(searchable_text, policy.keywords.unsubscribe)),
        contains_deadline_language=bool(_match_keywords(searchable_text, policy.keywords.deadline)),
        detected_dates=_extract_deadline_dates(
            searchable_text,
            policy.keywords.deadline,
            message.received_at,
        ),
        has_attachments=message.has_attachments,
        provider_marked_high_importance=message.importance is Importance.HIGH,
        provider_marked_low_importance=message.importance is Importance.LOW,
        external_sender=not trusted_domain and not trusted_sender,
        empty_subject=not bool(message.subject),
        sender_mismatch=(
            message.sender_address is not None and message.sender_address != message.from_address
        ),
    )


def _format_matches(values: tuple[str, ...]) -> str | None:
    """Join matched values within ScoreReason's storage limit."""

    if not values:
        return None
    return ", ".join(values)[:320]


class RuleEngine:
    """Evaluate normalized messages using one immutable rule policy."""

    def __init__(self, policy: RulePolicy) -> None:
        self.policy = policy

    @classmethod
    def from_yaml(cls, path: str | Path) -> RuleEngine:
        """Construct an engine from a validated YAML policy."""

        return cls(load_policy(path))

    def extract_features(self, message: NormalizedMessage) -> MessageFeatures:
        """Expose feature extraction for diagnostics and tests."""

        return extract_features(message, self.policy)

    def evaluate(self, message: NormalizedMessage) -> RuleEvaluation:
        """Score one normalized message and return an auditable decision."""

        features = self.extract_features(message)
        weights = self.policy.weights
        reasons: list[ScoreReason] = []

        def add_reason(
            code: str,
            description: str,
            score_change: int,
            matched_value: str | None = None,
        ) -> None:
            reasons.append(
                ScoreReason(
                    code=code,
                    description=description,
                    score_change=score_change,
                    matched_value=matched_value,
                )
            )

        if features.sender_is_trusted:
            add_reason(
                "trusted_sender",
                "发件人在可信联系人名单中",
                weights.trusted_sender,
                message.from_address,
            )
        elif features.sender_domain_is_trusted:
            add_reason(
                "trusted_domain",
                "发件人来自可信学校域名",
                weights.trusted_domain,
                message.from_domain,
            )

        if features.directly_addressed:
            add_reason(
                "direct_recipient",
                "邮件直接发送到用户邮箱",
                weights.direct_recipient,
            )
        if features.looks_like_bulk_mail:
            add_reason("bulk_mail", "收件地址表现为群发列表", weights.bulk_mail)

        if features.security_keywords:
            add_reason(
                "security_keyword",
                "检测到信息安全相关关键词",
                weights.security_keyword,
                _format_matches(features.security_keywords),
            )
        elif features.urgent_keywords:
            add_reason(
                "urgent_keyword",
                "检测到紧急或必须处理的关键词",
                weights.urgent_keyword,
                _format_matches(features.urgent_keywords),
            )

        if features.contains_deadline_language:
            future_dates = tuple(
                value for value in features.detected_dates if value >= message.received_at
            )
            if future_dates:
                next_deadline = min(future_dates)
                seconds_remaining = (next_deadline - message.received_at).total_seconds()
                if seconds_remaining <= 2 * 24 * 60 * 60:
                    deadline_code = "deadline_within_two_days"
                    deadline_description = "检测到两天内截止时间"
                    deadline_weight = weights.deadline_within_two_days
                elif seconds_remaining <= 7 * 24 * 60 * 60:
                    deadline_code = "deadline_within_seven_days"
                    deadline_description = "检测到七天内截止时间"
                    deadline_weight = weights.deadline_within_seven_days
                else:
                    deadline_code = "deadline_later"
                    deadline_description = "检测到七天后的截止时间"
                    deadline_weight = weights.deadline_later
                add_reason(
                    deadline_code,
                    deadline_description,
                    deadline_weight,
                    next_deadline.isoformat(),
                )
            else:
                add_reason(
                    "deadline_without_date",
                    "检测到截止语言，但未提取到未来的明确日期",
                    weights.deadline_without_date,
                )

        if features.action_keywords:
            add_reason(
                "action_keyword",
                "检测到需要用户采取行动的关键词",
                weights.action_keyword,
                _format_matches(features.action_keywords),
            )
        if features.opportunity_keywords:
            add_reason(
                "opportunity_keyword",
                "检测到学习或职业机会关键词",
                weights.opportunity_keyword,
                _format_matches(features.opportunity_keywords),
            )
        if features.bulk_keywords:
            add_reason(
                "bulk_keyword",
                "检测到活动、简报或推广关键词",
                weights.bulk_keyword,
                _format_matches(features.bulk_keywords),
            )
        if features.contains_unsubscribe:
            add_reason("unsubscribe", "邮件包含退订信息", weights.unsubscribe)
        if features.no_action_keywords:
            add_reason(
                "no_action_required",
                "邮件明确表示无需立即行动",
                weights.no_action_required,
                _format_matches(features.no_action_keywords),
            )

        if features.provider_marked_high_importance:
            add_reason("high_importance", "邮件提供方标记为高重要性", weights.high_importance)
        elif features.provider_marked_low_importance:
            add_reason("low_importance", "邮件提供方标记为低重要性", weights.low_importance)
        if features.has_attachments:
            add_reason("has_attachment", "邮件包含附件", weights.has_attachment)
        if features.external_sender:
            add_reason("external_sender", "发件人不属于可信学校域名", weights.external_sender)
        if features.empty_subject:
            add_reason("empty_subject", "邮件缺少标题", weights.empty_subject)
        if features.sender_mismatch:
            add_reason(
                "sender_mismatch",
                "显示发件人与实际发送账号不同",
                weights.sender_mismatch,
                message.sender_address,
            )

        raw_score = self.policy.base_score + sum(reason.score_change for reason in reasons)
        final_score = max(0, min(100, raw_score))
        priority = self._priority_for_score(final_score)
        threshold_values = (
            self.policy.thresholds.p1,
            self.policy.thresholds.p2,
            self.policy.thresholds.p3,
            self.policy.thresholds.p4,
        )
        near_threshold = self.policy.review_margin > 0 and any(
            abs(final_score - threshold) <= self.policy.review_margin
            for threshold in threshold_values
        )
        conflicting_bulk_deadline = bool(
            features.bulk_keywords
            and features.contains_deadline_language
            and not features.urgent_keywords
            and not features.security_keywords
        )

        return RuleEvaluation(
            base_score=self.policy.base_score,
            final_score=final_score,
            suggested_priority=priority,
            reasons=tuple(reasons),
            requires_review=(features.empty_subject or conflicting_bulk_deadline or near_threshold),
        )

    def _priority_for_score(self, score: int) -> Priority:
        """Map a clamped score to a configured priority threshold."""

        thresholds = self.policy.thresholds
        if score >= thresholds.p1:
            return Priority.P1
        if score >= thresholds.p2:
            return Priority.P2
        if score >= thresholds.p3:
            return Priority.P3
        if score >= thresholds.p4:
            return Priority.P4
        return Priority.P5
