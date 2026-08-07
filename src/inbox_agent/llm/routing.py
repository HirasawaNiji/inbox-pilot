"""Explainable routing for deciding when structured LLM analysis is useful."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, ValidationError, model_validator
from yaml import YAMLError

from inbox_agent.models import FrozenModel, MessageFeatures, TriageResult


class LLMRoutingMode(StrEnum):
    """Supported provider invocation strategies."""

    SELECTIVE = "selective"
    ALL = "all"


class LLMRoutingReasonCode(StrEnum):
    """Stable reason codes explaining a route or skip decision."""

    FULL_EVALUATION = "full_evaluation"
    LOW_RULE_CONFIDENCE = "low_rule_confidence"
    RULE_REQUIRES_REVIEW = "rule_requires_review"
    AMBIGUOUS_DEADLINE = "ambiguous_deadline"
    MULTIPLE_DEADLINES = "multiple_deadlines"
    ACTION_NO_ACTION_CONFLICT = "action_no_action_conflict"
    IMPORTANCE_CONTENT_CONFLICT = "importance_content_conflict"
    URGENT_NO_ACTION_CONFLICT = "urgent_no_action_conflict"
    HIGH_CONFIDENCE_RULE = "high_confidence_rule"


class LLMRoutingPolicy(FrozenModel):
    """Validated switches and thresholds controlling provider invocation."""

    routing_version: str = Field(
        default="llm-routing-v1",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=100,
    )
    mode: LLMRoutingMode = LLMRoutingMode.SELECTIVE
    confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    route_on_rule_review: bool = True
    route_on_ambiguous_deadline: bool = True
    route_on_multiple_deadlines: bool = True
    route_on_action_no_action_conflict: bool = True
    route_on_importance_content_conflict: bool = True
    route_on_urgent_no_action_conflict: bool = True


class LLMRoutingReason(FrozenModel):
    """One auditable reason contributing to a route decision."""

    code: LLMRoutingReasonCode
    description: str = Field(min_length=1, max_length=500)
    evidence: str | None = Field(default=None, max_length=1_000)


class LLMRoutingDecision(FrozenModel):
    """Whether one message should receive structured LLM analysis."""

    message_id: str = Field(min_length=1, max_length=512)
    should_analyze: bool
    rule_confidence: float = Field(ge=0.0, le=1.0)
    routing_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=100,
    )
    reasons: tuple[LLMRoutingReason, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_reason_codes(self) -> Self:
        """Prevent duplicate explanations in one routing decision."""

        codes = [reason.code for reason in self.reasons]
        if len(codes) != len(set(codes)):
            raise ValueError("LLM routing decision contains duplicate reason codes")
        return self


class LLMRoutingPolicyError(Exception):
    """Base class for LLM routing-policy loading failures."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class LLMRoutingPolicyNotFoundError(LLMRoutingPolicyError):
    """Raised when an LLM routing-policy file is missing."""


class LLMRoutingPolicyReadError(LLMRoutingPolicyError):
    """Raised when an LLM routing-policy file cannot be read."""


class LLMRoutingPolicyYAMLError(LLMRoutingPolicyError):
    """Raised when an LLM routing-policy file contains invalid YAML."""


class LLMRoutingPolicyValidationError(LLMRoutingPolicyError):
    """Raised when YAML violates the LLM routing-policy schema."""

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(path, "LLM routing policy does not match the InboxPilot schema")


def load_llm_routing_policy(path: str | Path) -> LLMRoutingPolicy:
    """Read and validate one UTF-8 LLM routing policy."""

    policy_path = Path(path)
    try:
        raw_content = policy_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise LLMRoutingPolicyNotFoundError(
            policy_path,
            "LLM routing policy file does not exist",
        ) from error
    except OSError as error:
        raise LLMRoutingPolicyReadError(
            policy_path,
            "Unable to read LLM routing policy file",
        ) from error

    try:
        payload = yaml.safe_load(raw_content)
    except YAMLError as error:
        raise LLMRoutingPolicyYAMLError(
            policy_path,
            "LLM routing policy contains invalid YAML",
        ) from error

    try:
        return LLMRoutingPolicy.model_validate(payload)
    except ValidationError as error:
        raise LLMRoutingPolicyValidationError(policy_path, error) from error


def _joined(values: tuple[str, ...]) -> str | None:
    """Build bounded evidence text from matched keywords."""

    return ", ".join(values)[:1_000] or None


class LLMRouter:
    """Apply a deterministic policy to rule results and extracted signals."""

    def __init__(self, policy: LLMRoutingPolicy | None = None) -> None:
        self.policy = policy or LLMRoutingPolicy()

    @classmethod
    def from_yaml(cls, path: str | Path) -> LLMRouter:
        """Construct a router from a validated YAML policy."""

        return cls(load_llm_routing_policy(path))

    @classmethod
    def analyze_all(cls) -> LLMRouter:
        """Construct an explicit full-analysis router for evaluation runs."""

        return cls(LLMRoutingPolicy(mode=LLMRoutingMode.ALL))

    def decide(
        self,
        result: TriageResult,
        features: MessageFeatures,
    ) -> LLMRoutingDecision:
        """Return an explainable selective route decision for one message."""

        policy = self.policy
        if policy.mode is LLMRoutingMode.ALL:
            return LLMRoutingDecision(
                message_id=result.message_id,
                should_analyze=True,
                rule_confidence=result.confidence,
                routing_version=policy.routing_version,
                reasons=(
                    LLMRoutingReason(
                        code=LLMRoutingReasonCode.FULL_EVALUATION,
                        description="全量评测模式要求调用 LLM Provider",
                    ),
                ),
            )

        reasons: list[LLMRoutingReason] = []

        if result.confidence < policy.confidence_threshold:
            reasons.append(
                LLMRoutingReason(
                    code=LLMRoutingReasonCode.LOW_RULE_CONFIDENCE,
                    description="规则置信度低于 LLM 路由阈值",
                    evidence=(
                        f"confidence={result.confidence:.2f}, "
                        f"threshold={policy.confidence_threshold:.2f}"
                    ),
                )
            )
        if policy.route_on_rule_review and result.requires_review:
            reasons.append(
                LLMRoutingReason(
                    code=LLMRoutingReasonCode.RULE_REQUIRES_REVIEW,
                    description="规则结果要求人工复核",
                )
            )
        if (
            policy.route_on_ambiguous_deadline
            and features.contains_deadline_language
            and not features.detected_dates
        ):
            reasons.append(
                LLMRoutingReason(
                    code=LLMRoutingReasonCode.AMBIGUOUS_DEADLINE,
                    description="检测到截止语言但规则未提取出明确日期",
                )
            )
        if policy.route_on_multiple_deadlines and len(features.detected_dates) > 1:
            reasons.append(
                LLMRoutingReason(
                    code=LLMRoutingReasonCode.MULTIPLE_DEADLINES,
                    description="规则检测到多个候选截止时间",
                    evidence=", ".join(value.isoformat() for value in features.detected_dates)[
                        :1_000
                    ],
                )
            )
        if (
            policy.route_on_action_no_action_conflict
            and features.action_keywords
            and features.no_action_keywords
        ):
            reasons.append(
                LLMRoutingReason(
                    code=LLMRoutingReasonCode.ACTION_NO_ACTION_CONFLICT,
                    description="邮件同时包含行动要求和无需行动表述",
                    evidence=_joined(features.action_keywords + features.no_action_keywords),
                )
            )
        if (
            policy.route_on_importance_content_conflict
            and features.provider_marked_high_importance
            and (
                features.contains_unsubscribe
                or features.bulk_keywords
                or features.no_action_keywords
            )
        ):
            reasons.append(
                LLMRoutingReason(
                    code=LLMRoutingReasonCode.IMPORTANCE_CONTENT_CONFLICT,
                    description="高重要性标记与推广、群发或无需行动内容冲突",
                    evidence=_joined(features.bulk_keywords + features.no_action_keywords),
                )
            )
        if (
            policy.route_on_urgent_no_action_conflict
            and (features.urgent_keywords or features.security_keywords)
            and features.no_action_keywords
        ):
            reasons.append(
                LLMRoutingReason(
                    code=LLMRoutingReasonCode.URGENT_NO_ACTION_CONFLICT,
                    description="紧急或安全信号与无需行动表述冲突",
                    evidence=_joined(
                        features.urgent_keywords
                        + features.security_keywords
                        + features.no_action_keywords
                    ),
                )
            )

        should_analyze = bool(reasons)
        if not should_analyze:
            reasons.append(
                LLMRoutingReason(
                    code=LLMRoutingReasonCode.HIGH_CONFIDENCE_RULE,
                    description="规则结果置信度充足且未检测到冲突信号",
                    evidence=f"confidence={result.confidence:.2f}",
                )
            )

        return LLMRoutingDecision(
            message_id=result.message_id,
            should_analyze=should_analyze,
            rule_confidence=result.confidence,
            routing_version=policy.routing_version,
            reasons=tuple(reasons),
        )
