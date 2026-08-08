"""Offline orchestration for loading, normalizing, and triaging email datasets."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from inbox_agent.llm.fusion import LLMFusionDecision, LLMFusionEngine
from inbox_agent.llm.provider import (
    LLMProvider,
    LLMProviderResultMismatchError,
)
from inbox_agent.llm.routing import LLMRouter, LLMRoutingDecision
from inbox_agent.loader import load_dataset
from inbox_agent.models import (
    DecisionSource,
    EmailMessage,
    FrozenModel,
    LLMAnalysisResult,
    MessageDataset,
    MessageFeatures,
    NormalizedMessage,
    Priority,
    RuleEvaluation,
    TriageResult,
)
from inbox_agent.normalizer import normalize_message
from inbox_agent.rule_engine import RuleEngine

_PRIORITY_SORT_ORDER = {
    Priority.P1: 0,
    Priority.P2: 1,
    Priority.P3: 2,
    Priority.P4: 3,
    Priority.P5: 4,
}


class AnalysisFailure(FrozenModel):
    """One isolated failure that did not stop the remaining dataset."""

    message_id: str
    stage: str
    error_type: str
    error_message: str


class AnalysisReport(FrozenModel):
    """Complete output of one offline dataset analysis run."""

    schema_version: str
    policy_version: str
    evaluated_at: datetime
    results: tuple[TriageResult, ...] = ()
    rule_evaluations: tuple[RuleEvaluation, ...] = ()
    failures: tuple[AnalysisFailure, ...] = ()
    llm_analyses: tuple[LLMAnalysisResult, ...] = ()
    llm_failures: tuple[AnalysisFailure, ...] = ()
    llm_routing_decisions: tuple[LLMRoutingDecision, ...] = ()
    llm_fusion_decisions: tuple[LLMFusionDecision, ...] = ()

    @property
    def processed_count(self) -> int:
        """Return the number of successfully evaluated messages."""

        return len(self.results)

    @property
    def failure_count(self) -> int:
        """Return the number of isolated failures."""

        return len(self.failures)

    @property
    def review_count(self) -> int:
        """Return the number of successful results requiring review."""

        return sum(result.requires_review for result in self.results)

    @property
    def llm_analysis_count(self) -> int:
        """Return the number of successful sidecar LLM analyses."""

        return len(self.llm_analyses)

    @property
    def llm_failure_count(self) -> int:
        """Return the number of isolated sidecar LLM failures."""

        return len(self.llm_failures)

    @property
    def llm_routed_count(self) -> int:
        """Return how many messages the router selected for LLM analysis."""

        return sum(decision.should_analyze for decision in self.llm_routing_decisions)

    @property
    def llm_skipped_count(self) -> int:
        """Return how many messages the router skipped."""

        return sum(not decision.should_analyze for decision in self.llm_routing_decisions)

    @property
    def llm_fused_count(self) -> int:
        """Return how many successful analyses changed public results."""

        return sum(decision.applied for decision in self.llm_fusion_decisions)

    @property
    def llm_sidecar_only_count(self) -> int:
        """Return how many successful analyses remained sidecar-only."""

        return sum(not decision.applied for decision in self.llm_fusion_decisions)


def _searchable_text(message: NormalizedMessage) -> str:
    """Build the normalized text used by deterministic category rules."""

    return "\n".join((message.subject, message.body_text, message.body_preview)).lower()


def infer_category(message: NormalizedMessage, features: MessageFeatures) -> str:
    """Infer one stable phase-one category from explainable message signals."""

    text = _searchable_text(message)

    if features.security_keywords:
        return "security_alert"
    if features.empty_subject:
        return "incomplete_message"
    if features.external_sender and features.contains_unsubscribe:
        return "promotion"
    if features.contains_unsubscribe:
        return "newsletter"
    if "考试" in text and (features.urgent_keywords or "变更" in text):
        return "exam_change"
    if "课程取消" in text or ("课程" in text and "取消" in text):
        return "course_change"
    if "奖学金" in text:
        return "scholarship_deadline"
    if "学费" in text or "缴费" in text:
        return "payment_deadline"
    if "图书" in text or "续借" in text:
        return "library_reminder"
    if "校历" in text:
        return "academic_calendar"
    if "注册信息" in text or ("学期注册" in text and features.contains_deadline_language):
        return "administrative_deadline"
    if "选课" in text:
        return "course_registration"
    if any(keyword in text for keyword in ("作业", "课程项目", "课程平台")) and (
        features.action_keywords or features.urgent_keywords
    ):
        return "academic_deadline"
    if "阅读材料" in text or "提前阅读" in text:
        return "course_material"
    if any(keyword in text for keyword in ("假期愉快", "节日愉快", "感谢大家")):
        return "courtesy_message"
    if any(keyword in text for keyword in ("招聘", "双选会", "实习")):
        return "career_event"
    if features.bulk_keywords and features.contains_deadline_language:
        return "event_registration"
    if features.bulk_keywords:
        return "campus_activity"
    return "general_notice"


def _build_summary(message: NormalizedMessage, maximum_length: int = 200) -> str:
    """Build a non-empty deterministic summary without calling an LLM."""

    summary = message.subject or message.body_preview or message.body_text or "无标题邮件"
    if len(summary) <= maximum_length:
        return summary
    return f"{summary[: maximum_length - 1].rstrip()}…"


def _future_deadline(
    message: NormalizedMessage,
    features: MessageFeatures,
) -> datetime | None:
    """Return the earliest extracted deadline that is not already past."""

    future_dates = tuple(value for value in features.detected_dates if value >= message.received_at)
    return min(future_dates) if future_dates else None


def _rule_confidence(requires_review: bool) -> float:
    """Return a deliberately simple phase-one rule confidence value."""

    return 0.6 if requires_review else 0.9


def _failure_record(
    message_id: str,
    stage: str,
    error: Exception,
) -> AnalysisFailure:
    """Convert an exception into one bounded, serializable failure."""

    return AnalysisFailure(
        message_id=message_id,
        stage=stage,
        error_type=type(error).__name__,
        error_message=str(error)[:500] or "Unknown analysis error",
    )


class OfflinePipeline:
    """Coordinate deterministic analysis while isolating per-message failures."""

    def __init__(
        self,
        engine: RuleEngine,
        llm_provider: LLMProvider | None = None,
        llm_router: LLMRouter | None = None,
        llm_fusion: LLMFusionEngine | None = None,
    ) -> None:
        self.engine = engine
        self.llm_provider = llm_provider
        self.llm_router = llm_router or (LLMRouter() if llm_provider is not None else None)
        self.llm_fusion = llm_fusion or (LLMFusionEngine() if llm_provider is not None else None)

    @classmethod
    def from_yaml(
        cls,
        policy_path: str | Path,
        *,
        llm_provider: LLMProvider | None = None,
        llm_router: LLMRouter | None = None,
        llm_routing_path: str | Path | None = None,
        llm_fusion: LLMFusionEngine | None = None,
        llm_fusion_path: str | Path | None = None,
    ) -> OfflinePipeline:
        """Construct the pipeline from rule and optional routing policies."""

        if llm_router is not None and llm_routing_path is not None:
            raise ValueError("provide either llm_router or llm_routing_path, not both")
        if llm_fusion is not None and llm_fusion_path is not None:
            raise ValueError("provide either llm_fusion or llm_fusion_path, not both")
        configured_router = (
            LLMRouter.from_yaml(llm_routing_path) if llm_routing_path is not None else llm_router
        )
        configured_fusion = (
            LLMFusionEngine.from_yaml(llm_fusion_path)
            if llm_fusion_path is not None
            else llm_fusion
        )

        return cls(
            RuleEngine.from_yaml(policy_path),
            llm_provider,
            configured_router,
            configured_fusion,
        )

    def analyze_message(
        self,
        message: EmailMessage,
        evaluated_at: datetime,
    ) -> tuple[NormalizedMessage, TriageResult]:
        """Normalize and evaluate one validated provider message."""

        normalized, _, result = self._analyze_message_with_evaluation(message, evaluated_at)
        return normalized, result

    def _analyze_message_with_evaluation(
        self,
        message: EmailMessage,
        evaluated_at: datetime,
    ) -> tuple[NormalizedMessage, RuleEvaluation, TriageResult]:
        """Return one public result together with its private rule evidence."""

        normalized = normalize_message(message)
        features = self.engine.extract_features(normalized)
        evaluation = self.engine.evaluate(normalized)
        result = TriageResult(
            message_id=normalized.source_id,
            priority=evaluation.suggested_priority,
            score=evaluation.final_score,
            confidence=_rule_confidence(evaluation.requires_review),
            category=infer_category(normalized, features),
            summary=_build_summary(normalized),
            deadline=_future_deadline(normalized, features),
            reasons=evaluation.reasons,
            requires_review=evaluation.requires_review,
            decision_source=DecisionSource.RULE,
            evaluated_at=evaluated_at,
            policy_version=self.engine.policy.policy_version,
        )
        return normalized, evaluation, result

    def analyze_dataset(
        self,
        dataset: MessageDataset,
        *,
        evaluated_at: datetime | None = None,
        stop_on_llm_failure: bool = False,
    ) -> AnalysisReport:
        """Analyze all messages, keeping successful results when one fails."""

        run_time = evaluated_at or datetime.now(UTC)
        successful: list[tuple[datetime, TriageResult]] = []
        rule_evaluations: dict[str, RuleEvaluation] = {}
        failures: list[AnalysisFailure] = []
        llm_analyses: dict[str, LLMAnalysisResult] = {}
        llm_failures: list[AnalysisFailure] = []
        llm_routing_decisions: dict[str, LLMRoutingDecision] = {}
        llm_fusion_decisions: dict[str, LLMFusionDecision] = {}
        llm_stopped = False

        for message in dataset.messages:
            try:
                normalized, evaluation, result = self._analyze_message_with_evaluation(
                    message,
                    run_time,
                )
            except Exception as error:  # noqa: BLE001 - dataset isolation is intentional
                failures.append(_failure_record(message.source_id, "message_analysis", error))
                continue
            successful.append((normalized.received_at, result))
            rule_evaluations[normalized.source_id] = evaluation

            if self.llm_provider is not None and not llm_stopped:
                assert self.llm_router is not None
                try:
                    features = self.engine.extract_features(normalized)
                    routing_decision = self.llm_router.decide(result, features)
                except Exception as error:  # noqa: BLE001 - sidecar isolation is intentional
                    llm_failures.append(_failure_record(normalized.source_id, "llm_routing", error))
                    if stop_on_llm_failure:
                        llm_stopped = True
                    continue
                llm_routing_decisions[normalized.source_id] = routing_decision
                if not routing_decision.should_analyze:
                    continue

                try:
                    llm_result = self.llm_provider.analyze(normalized)
                    if llm_result.message_id != normalized.source_id:
                        raise LLMProviderResultMismatchError(
                            self.llm_provider.provider_name,
                            normalized.source_id,
                            f"returned message_id {llm_result.message_id!r}",
                        )
                except Exception as error:  # noqa: BLE001 - sidecar isolation is intentional
                    llm_failures.append(
                        _failure_record(normalized.source_id, "llm_analysis", error)
                    )
                    if stop_on_llm_failure:
                        llm_stopped = True
                else:
                    llm_analyses[normalized.source_id] = llm_result
                    assert self.llm_fusion is not None
                    try:
                        fused_result, fusion_decision = self.llm_fusion.fuse(
                            result,
                            llm_result,
                        )
                    except Exception as error:  # noqa: BLE001 - sidecar isolation is intentional
                        llm_failures.append(
                            _failure_record(normalized.source_id, "llm_fusion", error)
                        )
                        if stop_on_llm_failure:
                            llm_stopped = True
                    else:
                        successful[-1] = (normalized.received_at, fused_result)
                        llm_fusion_decisions[normalized.source_id] = fusion_decision

        successful.sort(
            key=lambda item: (
                _PRIORITY_SORT_ORDER[item[1].priority],
                -item[1].score,
                -item[0].timestamp(),
                item[1].message_id,
            )
        )

        return AnalysisReport(
            schema_version=dataset.schema_version,
            policy_version=self.engine.policy.policy_version,
            evaluated_at=run_time,
            results=tuple(result for _, result in successful),
            rule_evaluations=tuple(rule_evaluations[result.message_id] for _, result in successful),
            failures=tuple(failures),
            llm_analyses=tuple(
                llm_analyses[result.message_id]
                for _, result in successful
                if result.message_id in llm_analyses
            ),
            llm_failures=tuple(llm_failures),
            llm_routing_decisions=tuple(
                llm_routing_decisions[result.message_id]
                for _, result in successful
                if result.message_id in llm_routing_decisions
            ),
            llm_fusion_decisions=tuple(
                llm_fusion_decisions[result.message_id]
                for _, result in successful
                if result.message_id in llm_fusion_decisions
            ),
        )

    def analyze_file(
        self,
        dataset_path: str | Path,
        *,
        evaluated_at: datetime | None = None,
    ) -> AnalysisReport:
        """Load and analyze a JSON dataset from disk."""

        return self.analyze_dataset(
            load_dataset(dataset_path),
            evaluated_at=evaluated_at,
        )


def analyze_file(
    dataset_path: str | Path,
    policy_path: str | Path,
    *,
    evaluated_at: datetime | None = None,
    llm_provider: LLMProvider | None = None,
    llm_router: LLMRouter | None = None,
    llm_routing_path: str | Path | None = None,
    llm_fusion: LLMFusionEngine | None = None,
    llm_fusion_path: str | Path | None = None,
) -> AnalysisReport:
    """Convenience entry point used by the CLI and future integrations."""

    pipeline = OfflinePipeline.from_yaml(
        policy_path,
        llm_provider=llm_provider,
        llm_router=llm_router,
        llm_routing_path=llm_routing_path,
        llm_fusion=llm_fusion,
        llm_fusion_path=llm_fusion_path,
    )
    return pipeline.analyze_file(dataset_path, evaluated_at=evaluated_at)
