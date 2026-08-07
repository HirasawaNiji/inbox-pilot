"""Offline orchestration for loading, normalizing, and triaging email datasets."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from inbox_agent.loader import load_dataset
from inbox_agent.models import (
    DecisionSource,
    EmailMessage,
    FrozenModel,
    MessageDataset,
    MessageFeatures,
    NormalizedMessage,
    Priority,
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
    failures: tuple[AnalysisFailure, ...] = ()

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


class OfflinePipeline:
    """Coordinate deterministic analysis while isolating per-message failures."""

    def __init__(self, engine: RuleEngine) -> None:
        self.engine = engine

    @classmethod
    def from_yaml(cls, policy_path: str | Path) -> OfflinePipeline:
        """Construct the pipeline from one YAML rule policy."""

        return cls(RuleEngine.from_yaml(policy_path))

    def analyze_message(
        self,
        message: EmailMessage,
        evaluated_at: datetime,
    ) -> tuple[NormalizedMessage, TriageResult]:
        """Normalize and evaluate one validated provider message."""

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
        return normalized, result

    def analyze_dataset(
        self,
        dataset: MessageDataset,
        *,
        evaluated_at: datetime | None = None,
    ) -> AnalysisReport:
        """Analyze all messages, keeping successful results when one fails."""

        run_time = evaluated_at or datetime.now(UTC)
        successful: list[tuple[datetime, TriageResult]] = []
        failures: list[AnalysisFailure] = []

        for message in dataset.messages:
            try:
                normalized, result = self.analyze_message(message, run_time)
            except Exception as error:  # noqa: BLE001 - dataset isolation is intentional
                failures.append(
                    AnalysisFailure(
                        message_id=message.source_id,
                        stage="message_analysis",
                        error_type=type(error).__name__,
                        error_message=str(error)[:500] or "Unknown analysis error",
                    )
                )
                continue
            successful.append((normalized.received_at, result))

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
            failures=tuple(failures),
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
) -> AnalysisReport:
    """Convenience entry point used by the CLI and future integrations."""

    pipeline = OfflinePipeline.from_yaml(policy_path)
    return pipeline.analyze_file(dataset_path, evaluated_at=evaluated_at)
