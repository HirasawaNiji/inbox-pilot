"""Conservative fusion of deterministic rule and structured LLM results."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, ValidationError, model_validator
from yaml import YAMLError

from inbox_agent.models import (
    DecisionSource,
    FrozenModel,
    LLMAnalysisResult,
    Priority,
    TriageResult,
)

_PRIORITY_RANK = {
    Priority.P1: 1,
    Priority.P2: 2,
    Priority.P3: 3,
    Priority.P4: 4,
    Priority.P5: 5,
}


class LLMFusionMode(StrEnum):
    """Supported handling of a successful sidecar analysis."""

    CONSERVATIVE = "conservative"
    SIDECAR_ONLY = "sidecar_only"


class LLMFusionReasonCode(StrEnum):
    """Stable explanations for a fusion outcome."""

    SIDECAR_ONLY = "sidecar_only"
    LLM_BELOW_CONFIDENCE = "llm_below_confidence"
    STRUCTURED_FIELDS_ADOPTED = "structured_fields_adopted"
    PRIORITY_AGREEMENT = "priority_agreement"
    LLM_PRIORITY_UPGRADE = "llm_priority_upgrade"
    LLM_PRIORITY_UPGRADE_BLOCKED = "llm_priority_upgrade_blocked"
    LLM_PRIORITY_DOWNGRADE = "llm_priority_downgrade"
    LLM_PRIORITY_DOWNGRADE_BLOCKED = "llm_priority_downgrade_blocked"
    PRIORITY_DISAGREEMENT_REVIEW = "priority_disagreement_review"
    LLM_REVIEW_REQUESTED = "llm_review_requested"
    DEADLINE_ADDED = "deadline_added"
    DEADLINE_AGREEMENT = "deadline_agreement"
    DEADLINE_CONFLICT = "deadline_conflict"


class LLMFusionPolicy(FrozenModel):
    """Validated safety controls for rule and LLM decision fusion."""

    fusion_version: str = Field(
        default="llm-fusion-v1",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=100,
    )
    mode: LLMFusionMode = LLMFusionMode.CONSERVATIVE
    minimum_llm_confidence: float = Field(default=0.80, ge=0.0, le=1.0)
    allow_priority_upgrade: bool = True
    allow_priority_downgrade: bool = False
    force_review_on_priority_disagreement: bool = True
    force_review_on_deadline_conflict: bool = True
    deadline_tolerance_minutes: int = Field(default=0, ge=0, le=1_440)


class LLMFusionReason(FrozenModel):
    """One auditable explanation contributing to a fused decision."""

    code: LLMFusionReasonCode
    description: str = Field(min_length=1, max_length=500)
    evidence: str | None = Field(default=None, max_length=1_000)


class LLMFusionDecision(FrozenModel):
    """Traceable record of how rules and one LLM analysis were combined."""

    message_id: str = Field(min_length=1, max_length=512)
    applied: bool
    fusion_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=100,
    )
    rule_priority: Priority
    llm_priority: Priority
    final_priority: Priority
    rule_confidence: float = Field(ge=0.0, le=1.0)
    llm_confidence: float = Field(ge=0.0, le=1.0)
    final_requires_review: bool
    reasons: tuple[LLMFusionReason, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_reason_codes(self) -> Self:
        """Prevent duplicated fusion explanations."""

        codes = [reason.code for reason in self.reasons]
        if len(codes) != len(set(codes)):
            raise ValueError("LLM fusion decision contains duplicate reason codes")
        return self


class LLMFusionPolicyError(Exception):
    """Base class for LLM fusion-policy loading failures."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{message}: {path}")


class LLMFusionPolicyNotFoundError(LLMFusionPolicyError):
    """Raised when an LLM fusion-policy file is missing."""


class LLMFusionPolicyReadError(LLMFusionPolicyError):
    """Raised when an LLM fusion-policy file cannot be read."""


class LLMFusionPolicyYAMLError(LLMFusionPolicyError):
    """Raised when an LLM fusion-policy file contains invalid YAML."""


class LLMFusionPolicyValidationError(LLMFusionPolicyError):
    """Raised when YAML violates the LLM fusion-policy schema."""

    def __init__(self, path: Path, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__(path, "LLM fusion policy does not match the InboxPilot schema")


def load_llm_fusion_policy(path: str | Path) -> LLMFusionPolicy:
    """Read and validate one UTF-8 LLM fusion policy."""

    policy_path = Path(path)
    try:
        raw_content = policy_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise LLMFusionPolicyNotFoundError(
            policy_path,
            "LLM fusion policy file does not exist",
        ) from error
    except OSError as error:
        raise LLMFusionPolicyReadError(
            policy_path,
            "Unable to read LLM fusion policy file",
        ) from error

    try:
        payload = yaml.safe_load(raw_content)
    except YAMLError as error:
        raise LLMFusionPolicyYAMLError(
            policy_path,
            "LLM fusion policy contains invalid YAML",
        ) from error

    try:
        return LLMFusionPolicy.model_validate(payload)
    except ValidationError as error:
        raise LLMFusionPolicyValidationError(policy_path, error) from error


def _priority_relation(rule_priority: Priority, llm_priority: Priority) -> int:
    """Return -1 for LLM upgrade, 0 for agreement, and 1 for downgrade."""

    rule_rank = _PRIORITY_RANK[rule_priority]
    llm_rank = _PRIORITY_RANK[llm_priority]
    if llm_rank < rule_rank:
        return -1
    if llm_rank > rule_rank:
        return 1
    return 0


def _priority_evidence(rule_priority: Priority, llm_priority: Priority) -> str:
    """Format both priority values for auditable fusion reasons."""

    return f"rule={rule_priority.value}, llm={llm_priority.value}"


class LLMFusionEngine:
    """Apply conservative, deterministic fusion to one successful analysis."""

    def __init__(self, policy: LLMFusionPolicy | None = None) -> None:
        self.policy = policy or LLMFusionPolicy()

    @classmethod
    def from_yaml(cls, path: str | Path) -> LLMFusionEngine:
        """Construct a fusion engine from a validated YAML policy."""

        return cls(load_llm_fusion_policy(path))

    @classmethod
    def sidecar_only(cls) -> LLMFusionEngine:
        """Construct a no-fusion engine for isolated LLM evaluation."""

        return cls(LLMFusionPolicy(mode=LLMFusionMode.SIDECAR_ONLY))

    def fuse(
        self,
        rule_result: TriageResult,
        llm_result: LLMAnalysisResult,
    ) -> tuple[TriageResult, LLMFusionDecision]:
        """Combine one rule result and matching structured LLM analysis."""

        if llm_result.message_id != rule_result.message_id:
            raise ValueError(
                "cannot fuse different message IDs: "
                f"{rule_result.message_id!r} and {llm_result.message_id!r}"
            )

        policy = self.policy
        analysis = llm_result.analysis
        if policy.mode is LLMFusionMode.SIDECAR_ONLY:
            reason = LLMFusionReason(
                code=LLMFusionReasonCode.SIDECAR_ONLY,
                description="旁路模式保留规则结果，不应用 LLM 判断",
            )
            return rule_result, LLMFusionDecision(
                message_id=rule_result.message_id,
                applied=False,
                fusion_version=policy.fusion_version,
                rule_priority=rule_result.priority,
                llm_priority=analysis.priority,
                final_priority=rule_result.priority,
                rule_confidence=rule_result.confidence,
                llm_confidence=analysis.confidence,
                final_requires_review=rule_result.requires_review,
                reasons=(reason,),
            )

        reasons: list[LLMFusionReason] = []
        requires_review = rule_result.requires_review or analysis.requires_review
        final_priority = rule_result.priority
        final_category = rule_result.category
        final_summary = rule_result.summary
        final_action_items = rule_result.action_items
        final_deadline = rule_result.deadline
        llm_is_reliable = analysis.confidence >= policy.minimum_llm_confidence

        if analysis.requires_review:
            reasons.append(
                LLMFusionReason(
                    code=LLMFusionReasonCode.LLM_REVIEW_REQUESTED,
                    description="LLM 结构化结果明确要求人工复核",
                )
            )

        if not llm_is_reliable:
            requires_review = True
            reasons.append(
                LLMFusionReason(
                    code=LLMFusionReasonCode.LLM_BELOW_CONFIDENCE,
                    description="LLM 置信度低于融合阈值，保留规则字段",
                    evidence=(
                        f"confidence={analysis.confidence:.2f}, "
                        f"threshold={policy.minimum_llm_confidence:.2f}"
                    ),
                )
            )
        else:
            final_category = analysis.category.value
            final_summary = analysis.summary
            final_action_items = analysis.action_items
            reasons.append(
                LLMFusionReason(
                    code=LLMFusionReasonCode.STRUCTURED_FIELDS_ADOPTED,
                    description="采用高置信度 LLM 的类别、摘要和行动项",
                )
            )

            relation = _priority_relation(rule_result.priority, analysis.priority)
            if relation == 0:
                reasons.append(
                    LLMFusionReason(
                        code=LLMFusionReasonCode.PRIORITY_AGREEMENT,
                        description="规则与 LLM 的优先级一致",
                        evidence=rule_result.priority.value,
                    )
                )
            elif relation < 0:
                if policy.allow_priority_upgrade:
                    final_priority = analysis.priority
                    reasons.append(
                        LLMFusionReason(
                            code=LLMFusionReasonCode.LLM_PRIORITY_UPGRADE,
                            description="高置信度 LLM 将结果提升到更紧急优先级",
                            evidence=(f"{rule_result.priority.value} -> {analysis.priority.value}"),
                        )
                    )
                else:
                    reasons.append(
                        LLMFusionReason(
                            code=LLMFusionReasonCode.LLM_PRIORITY_UPGRADE_BLOCKED,
                            description="融合策略禁止 LLM 提升规则优先级",
                            evidence=_priority_evidence(
                                rule_result.priority,
                                analysis.priority,
                            ),
                        )
                    )
            elif policy.allow_priority_downgrade:
                final_priority = analysis.priority
                reasons.append(
                    LLMFusionReason(
                        code=LLMFusionReasonCode.LLM_PRIORITY_DOWNGRADE,
                        description="策略允许高置信度 LLM 降低规则优先级",
                        evidence=f"{rule_result.priority.value} -> {analysis.priority.value}",
                    )
                )
            else:
                reasons.append(
                    LLMFusionReason(
                        code=LLMFusionReasonCode.LLM_PRIORITY_DOWNGRADE_BLOCKED,
                        description="安全策略阻止 LLM 降低规则优先级",
                        evidence=_priority_evidence(
                            rule_result.priority,
                            analysis.priority,
                        ),
                    )
                )

            if relation != 0 and policy.force_review_on_priority_disagreement:
                requires_review = True
                reasons.append(
                    LLMFusionReason(
                        code=LLMFusionReasonCode.PRIORITY_DISAGREEMENT_REVIEW,
                        description="规则与 LLM 优先级不一致，强制人工复核",
                    )
                )

            llm_deadline = analysis.deadline.value if analysis.deadline is not None else None
            if rule_result.deadline is None and llm_deadline is not None:
                final_deadline = llm_deadline
                reasons.append(
                    LLMFusionReason(
                        code=LLMFusionReasonCode.DEADLINE_ADDED,
                        description="规则未提取截止时间，采用 LLM 截止时间",
                        evidence=llm_deadline.isoformat(),
                    )
                )
            elif rule_result.deadline is not None and llm_deadline is not None:
                difference_minutes = abs((rule_result.deadline - llm_deadline).total_seconds()) / 60
                if difference_minutes <= policy.deadline_tolerance_minutes:
                    final_deadline = min(rule_result.deadline, llm_deadline)
                    reasons.append(
                        LLMFusionReason(
                            code=LLMFusionReasonCode.DEADLINE_AGREEMENT,
                            description="规则与 LLM 截止时间在容差范围内一致",
                            evidence=final_deadline.isoformat(),
                        )
                    )
                else:
                    final_deadline = min(rule_result.deadline, llm_deadline)
                    if policy.force_review_on_deadline_conflict:
                        requires_review = True
                    reasons.append(
                        LLMFusionReason(
                            code=LLMFusionReasonCode.DEADLINE_CONFLICT,
                            description="规则与 LLM 截止时间冲突，保守采用较早时间",
                            evidence=(
                                f"rule={rule_result.deadline.isoformat()}, "
                                f"llm={llm_deadline.isoformat()}"
                            ),
                        )
                    )

        fused_result = rule_result.model_copy(
            update={
                "priority": final_priority,
                "confidence": min(rule_result.confidence, analysis.confidence),
                "category": final_category,
                "summary": final_summary,
                "action_items": final_action_items,
                "deadline": final_deadline,
                "requires_review": requires_review,
                "decision_source": DecisionSource.HYBRID,
            }
        )
        return fused_result, LLMFusionDecision(
            message_id=rule_result.message_id,
            applied=True,
            fusion_version=policy.fusion_version,
            rule_priority=rule_result.priority,
            llm_priority=analysis.priority,
            final_priority=final_priority,
            rule_confidence=rule_result.confidence,
            llm_confidence=analysis.confidence,
            final_requires_review=requires_review,
            reasons=tuple(reasons),
        )
