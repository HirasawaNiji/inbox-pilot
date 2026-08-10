"""Durable, idempotent synchronization-to-review workflow orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from inbox_agent.actions import (
    ActionAuditLog,
    ActionQueueRepository,
    audit_events_for_action,
    build_review_actions,
)
from inbox_agent.llm import LLMProvider
from inbox_agent.loader import load_dataset
from inbox_agent.models import MessageDataset
from inbox_agent.normalizer import normalize_message
from inbox_agent.observability import (
    EventOutcome,
    LLMPricingRate,
    ObservabilityEvent,
    ObservabilityRecorder,
    estimate_llm_cost,
    safe_message_hash,
)
from inbox_agent.pipeline import AnalysisFailure, AnalysisReport, OfflinePipeline
from inbox_agent.storage import (
    AnalysisRepository,
    Database,
    MailboxActionRepository,
    MessageRepository,
    UpsertOutcome,
    WorkflowRunRepository,
)
from inbox_agent.workflow.models import (
    DatasetSyncResult,
    WorkflowFailure,
    WorkflowReport,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)

Clock = Callable[[], datetime]
DatasetSync = Callable[[], DatasetSyncResult]
RunIdFactory = Callable[[], str]


class WorkflowExecutionError(RuntimeError):
    """Raised after an unexpected failure has been persisted for inspection."""

    def __init__(self, run_id: str, cause: Exception) -> None:
        self.run_id = run_id
        self.cause = cause
        super().__init__(f"workflow {run_id} failed: {cause}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _run_id() -> str:
    return f"run-{uuid4().hex}"


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_analysis_profile(
    policy_path: Path,
    *,
    llm_provider: LLMProvider | None = None,
    llm_routing_path: Path | None = None,
    llm_fusion_path: Path | None = None,
) -> str:
    """Hash every configuration input that can change a classification decision."""

    payload: dict[str, str | None] = {
        "policy": _file_digest(policy_path),
        "provider": llm_provider.provider_name if llm_provider is not None else None,
        "model": llm_provider.model_name if llm_provider is not None else None,
        "prompt": llm_provider.prompt_version if llm_provider is not None else None,
        "routing": _file_digest(llm_routing_path) if llm_routing_path is not None else None,
        "fusion": _file_digest(llm_fusion_path) if llm_fusion_path is not None else None,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _failure(value: AnalysisFailure) -> WorkflowFailure:
    return WorkflowFailure(
        message_id=value.message_id,
        stage=value.stage,
        error_type=value.error_type,
        error_message=value.error_message,
    )


def _filtered_analysis(report: AnalysisReport, allowed_ids: set[str]) -> AnalysisReport:
    result_ids = {
        result.message_id for result in report.results if result.message_id in allowed_ids
    }
    result_by_id = {result.message_id: result for result in report.results}
    rule_by_id = {
        result.message_id: rule
        for result, rule in zip(report.results, report.rule_evaluations, strict=True)
    }
    ordered_ids = tuple(
        result.message_id for result in report.results if result.message_id in result_ids
    )
    return report.model_copy(
        update={
            "results": tuple(result_by_id[message_id] for message_id in ordered_ids),
            "rule_evaluations": tuple(rule_by_id[message_id] for message_id in ordered_ids),
            "llm_analyses": tuple(
                value for value in report.llm_analyses if value.message_id in result_ids
            ),
            "llm_routing_decisions": tuple(
                value for value in report.llm_routing_decisions if value.message_id in result_ids
            ),
            "llm_fusion_decisions": tuple(
                value for value in report.llm_fusion_decisions if value.message_id in result_ids
            ),
            "failures": (),
            "llm_failures": (),
        }
    )


class _RunTracker:
    def __init__(
        self,
        repository: WorkflowRunRepository,
        run_id: str,
        started_at: datetime,
        clock: Clock,
        observability: ObservabilityRecorder | None = None,
    ) -> None:
        self.repository = repository
        self.run_id = run_id
        self.started_at = started_at
        self.clock = clock
        self.observability = observability
        self.steps: list[WorkflowStep] = []
        self.counters: dict[str, int] = {}
        self._save(WorkflowStatus.RUNNING, None, None)

    def start(self, name: str) -> None:
        self.steps.append(
            WorkflowStep(name=name, status=WorkflowStepStatus.RUNNING, started_at=self.clock())
        )
        self._save(WorkflowStatus.RUNNING, name, None)

    def finish(
        self,
        *,
        processed_count: int = 0,
        detail: str | None = None,
        skipped: bool = False,
    ) -> None:
        current = self.steps[-1]
        finished_at = self.clock()
        self.steps[-1] = current.model_copy(
            update={
                "status": WorkflowStepStatus.SKIPPED if skipped else WorkflowStepStatus.COMPLETED,
                "finished_at": finished_at,
                "processed_count": processed_count,
                "detail": detail,
            }
        )
        self._save(WorkflowStatus.RUNNING, None, None)
        self.record(
            ObservabilityEvent(
                occurred_at=finished_at,
                run_id=self.run_id,
                component="workflow",
                operation=current.name,
                outcome=EventOutcome.SKIPPED if skipped else EventOutcome.SUCCEEDED,
                duration_ms=max(
                    0, round((finished_at - current.started_at).total_seconds() * 1_000)
                ),
                details={"processed_count": processed_count, "detail": detail},
            )
        )

    def fail(self, error: Exception) -> None:
        if self.steps and self.steps[-1].status is WorkflowStepStatus.RUNNING:
            current = self.steps[-1]
            finished_at = self.clock()
            self.steps[-1] = current.model_copy(
                update={
                    "status": WorkflowStepStatus.FAILED,
                    "finished_at": finished_at,
                    "detail": str(error)[:500] or type(error).__name__,
                }
            )
            self.record(
                ObservabilityEvent(
                    occurred_at=finished_at,
                    run_id=self.run_id,
                    component="workflow",
                    operation=current.name,
                    outcome=EventOutcome.FAILED,
                    duration_ms=max(
                        0,
                        round((finished_at - current.started_at).total_seconds() * 1_000),
                    ),
                    error_type=type(error).__name__,
                )
            )

    def complete(self, status: WorkflowStatus, finished_at: datetime) -> None:
        self._save(status, None, finished_at)

    def failed(self, error: Exception, finished_at: datetime) -> None:
        self.fail(error)
        self._save(WorkflowStatus.FAILED, None, finished_at, str(error)[:1_000])
        self.record(
            ObservabilityEvent(
                occurred_at=finished_at,
                run_id=self.run_id,
                component="workflow",
                operation="workflow_run",
                outcome=EventOutcome.FAILED,
                duration_ms=max(
                    0,
                    round((finished_at - self.started_at).total_seconds() * 1_000),
                ),
                error_type=type(error).__name__,
            )
        )

    def record(self, event: ObservabilityEvent) -> None:
        """Keep observability failures from changing workflow semantics."""

        if self.observability is None:
            return
        try:
            self.observability.record(event)
        except Exception:  # noqa: BLE001 - telemetry is explicitly best effort
            return

    def _save(
        self,
        status: WorkflowStatus,
        current_step: str | None,
        finished_at: datetime | None,
        error_summary: str | None = None,
    ) -> None:
        self.repository.save(
            run_id=self.run_id,
            status=status.value,
            current_step=current_step,
            started_at=self.started_at.isoformat(),
            finished_at=finished_at.isoformat() if finished_at is not None else None,
            counters=self.counters,
            steps=tuple(step.model_dump(mode="json") for step in self.steps),
            error_summary=error_summary,
        )


class WorkflowOrchestrator:
    """Run safe incremental processing through a human-review action boundary."""

    def __init__(
        self,
        *,
        database: Database,
        pipeline: OfflinePipeline,
        analysis_profile: str,
        action_queue_path: Path,
        audit_log_path: Path,
        llm_provider: LLMProvider | None = None,
        clock: Clock = _utc_now,
        run_id_factory: RunIdFactory = _run_id,
        observability: ObservabilityRecorder | None = None,
        llm_pricing: tuple[LLMPricingRate, ...] = (),
    ) -> None:
        self.database = database
        self.pipeline = pipeline
        self.analysis_profile = analysis_profile
        self.action_queue_path = action_queue_path
        self.audit_log_path = audit_log_path
        self.llm_provider = llm_provider
        self.clock = clock
        self.run_id_factory = run_id_factory
        self.observability = observability
        self.llm_pricing = llm_pricing

    def run(
        self,
        dataset_path: Path,
        *,
        force: bool = False,
        dataset_sync: DatasetSync | None = None,
    ) -> WorkflowReport:
        run_id = self.run_id_factory()
        started_at = self.clock()
        tracker = _RunTracker(
            WorkflowRunRepository(self.database),
            run_id,
            started_at,
            self.clock,
            self.observability,
        )
        try:
            active_dataset_path, sync_failures = self._sync_step(
                tracker, dataset_path, dataset_sync
            )
            tracker.start("load_dataset")
            dataset = load_dataset(active_dataset_path)
            tracker.finish(processed_count=len(dataset.messages))

            import_result = self._import_step(tracker, dataset, force)
            eligible_dataset = MessageDataset(messages=import_result["eligible"])

            tracker.start("analyze_messages")
            analysis = self.pipeline.analyze_dataset(eligible_dataset)
            tracker.finish(
                processed_count=analysis.processed_count,
                detail=f"llm_failures={analysis.llm_failure_count}",
            )
            self._record_analysis_events(tracker, analysis)

            persist_result = self._persist_step(tracker, eligible_dataset, analysis)
            action_result = self._action_step(
                tracker,
                eligible_dataset,
                analysis,
                persist_result["complete_ids"],
            )

            analysis_failures = (
                tuple(import_result["failures"])
                + tuple(_failure(value) for value in analysis.failures)
                + sync_failures
            )
            llm_failures = tuple(_failure(value) for value in analysis.llm_failures)
            status = (
                WorkflowStatus.COMPLETED_WITH_FAILURES
                if analysis_failures or llm_failures
                else WorkflowStatus.COMPLETED
            )
            finished_at = self.clock()
            counters = {
                "total_messages": len(dataset.messages),
                "eligible_messages": len(eligible_dataset.messages),
                "analyzed_messages": analysis.processed_count,
                "analysis_failures": len(analysis_failures),
                "llm_failures": len(llm_failures),
                "actions_added": action_result["added"],
                "graph_write_request_count": 0,
            }
            tracker.counters.update(counters)
            tracker.complete(status, finished_at)
            tracker.record(
                ObservabilityEvent(
                    occurred_at=finished_at,
                    run_id=run_id,
                    component="workflow",
                    operation="workflow_run",
                    outcome=(
                        EventOutcome.SUCCEEDED
                        if status is WorkflowStatus.COMPLETED
                        else EventOutcome.COMPLETED_WITH_FAILURES
                    ),
                    duration_ms=max(
                        0,
                        round((finished_at - started_at).total_seconds() * 1_000),
                    ),
                    details=counters,
                )
            )
            return WorkflowReport(
                run_id=run_id,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                dataset_path=active_dataset_path,
                database_path=self.database.path,
                analysis_profile=self.analysis_profile,
                llm_provider=(
                    self.llm_provider.provider_name if self.llm_provider is not None else None
                ),
                outlook_sync_requested=dataset_sync is not None,
                total_messages=len(dataset.messages),
                imported_created=import_result["created_count"],
                imported_updated=import_result["updated_count"],
                imported_unchanged=import_result["unchanged_count"],
                eligible_messages=len(eligible_dataset.messages),
                skipped_current=import_result["skipped_count"],
                analyzed_messages=analysis.processed_count,
                persisted_analyses=persist_result["persisted_count"],
                analysis_failures=analysis_failures,
                llm_failures=llm_failures,
                actions_generated=action_result["generated"],
                actions_added=action_result["added"],
                actions_skipped=action_result["skipped"],
                audit_events_added=action_result["audit_added"],
                steps=tuple(tracker.steps),
            )
        except Exception as error:
            finished_at = self.clock()
            tracker.failed(error, finished_at)
            if isinstance(error, WorkflowExecutionError):
                raise
            raise WorkflowExecutionError(run_id, error) from error

    def _sync_step(
        self,
        tracker: _RunTracker,
        dataset_path: Path,
        dataset_sync: DatasetSync | None,
    ) -> tuple[Path, tuple[WorkflowFailure, ...]]:
        tracker.start("outlook_sync")
        if dataset_sync is None:
            tracker.finish(detail="read-only synchronization not requested", skipped=True)
            return dataset_path, ()
        result = dataset_sync()
        tracker.finish(
            processed_count=result.created_count + result.updated_count + result.unchanged_count,
            detail=f"completed={str(result.completed).lower()}",
        )
        failures = (
            (
                WorkflowFailure(
                    message_id="outlook-sync",
                    stage="outlook_sync",
                    error_type="GraphSyncIncomplete",
                    error_message=f"read-only sync reported {result.failure_count} failure(s)",
                ),
            )
            if not result.completed or result.failure_count
            else ()
        )
        return result.dataset_path, failures

    def _import_step(
        self,
        tracker: _RunTracker,
        dataset: MessageDataset,
        force: bool,
    ) -> dict[str, Any]:
        tracker.start("import_messages")
        messages = MessageRepository(self.database)
        analyses = AnalysisRepository(self.database)
        eligible = []
        failures: list[WorkflowFailure] = []
        counts = {UpsertOutcome.CREATED: 0, UpsertOutcome.UPDATED: 0, UpsertOutcome.UNCHANGED: 0}
        skipped_count = 0
        for message in dataset.messages:
            result = messages.upsert(message)
            counts[result.outcome] += 1
            try:
                messages.save_normalized(normalize_message(message))
            except Exception as error:  # noqa: BLE001 - per-message isolation is intentional
                tracker.record(
                    ObservabilityEvent(
                        occurred_at=self.clock(),
                        run_id=tracker.run_id,
                        message_hash=safe_message_hash(message.source_id),
                        component="workflow",
                        operation="message_import",
                        outcome=EventOutcome.FAILED,
                        error_type=type(error).__name__,
                    )
                )
                failures.append(
                    WorkflowFailure(
                        message_id=message.source_id,
                        stage="normalization",
                        error_type=type(error).__name__,
                        error_message=str(error)[:500] or "Unknown normalization error",
                    )
                )
                continue
            tracker.record(
                ObservabilityEvent(
                    occurred_at=self.clock(),
                    run_id=tracker.run_id,
                    message_hash=safe_message_hash(message.source_id),
                    component="workflow",
                    operation="message_import",
                    outcome=EventOutcome.SUCCEEDED,
                    details={"upsert_outcome": result.outcome.value},
                )
            )
            current = analyses.has_current(
                message.source,
                message.source_id,
                self.analysis_profile,
            )
            if force or not current:
                eligible.append(message)
            else:
                skipped_count += 1
        tracker.finish(
            processed_count=len(dataset.messages),
            detail=f"eligible={len(eligible)}, skipped={skipped_count}",
        )
        return {
            "eligible": tuple(eligible),
            "failures": tuple(failures),
            "created_count": counts[UpsertOutcome.CREATED],
            "updated_count": counts[UpsertOutcome.UPDATED],
            "unchanged_count": counts[UpsertOutcome.UNCHANGED],
            "skipped_count": skipped_count,
        }

    def _persist_step(
        self,
        tracker: _RunTracker,
        dataset: MessageDataset,
        analysis: AnalysisReport,
    ) -> dict[str, Any]:
        tracker.start("persist_results")
        repository = AnalysisRepository(self.database)
        messages_by_id = {message.source_id: message for message in dataset.messages}
        rule_by_id = {
            result.message_id: rule
            for result, rule in zip(analysis.results, analysis.rule_evaluations, strict=True)
        }
        llm_by_id = {value.message_id: value for value in analysis.llm_analyses}
        incomplete_ids = {value.message_id for value in analysis.llm_failures}
        complete_ids: set[str] = set()
        persisted_count = 0
        for result in analysis.results:
            message = messages_by_id[result.message_id]
            complete = result.message_id not in incomplete_ids
            repository.save(
                source=message.source,
                result=result,
                rule_evaluation=rule_by_id[result.message_id],
                llm_analysis=llm_by_id.get(result.message_id),
                analysis_profile=self.analysis_profile,
                complete=complete,
            )
            persisted_count += 1
            if complete:
                complete_ids.add(result.message_id)
        tracker.finish(processed_count=persisted_count)
        return {"persisted_count": persisted_count, "complete_ids": complete_ids}

    def _action_step(
        self,
        tracker: _RunTracker,
        dataset: MessageDataset,
        analysis: AnalysisReport,
        complete_ids: set[str],
    ) -> dict[str, int]:
        tracker.start("build_review_actions")
        action_dataset = MessageDataset(
            messages=tuple(
                message for message in dataset.messages if message.source_id in complete_ids
            )
        )
        action_analysis = _filtered_analysis(analysis, complete_ids)
        actions = build_review_actions(action_dataset, action_analysis)
        update = ActionQueueRepository(self.action_queue_path).enqueue(actions)
        audit_events = tuple(
            event for action in actions for event in audit_events_for_action(action)
        )
        audit_update = ActionAuditLog(self.audit_log_path).append_unique(audit_events)
        messages_by_id = {message.source_id: message for message in action_dataset.messages}
        action_repository = MailboxActionRepository(self.database)
        for action in actions:
            message = messages_by_id[action.message_id]
            action_repository.upsert(source=message.source, action=action)
            tracker.record(
                ObservabilityEvent(
                    occurred_at=self.clock(),
                    run_id=tracker.run_id,
                    message_hash=safe_message_hash(action.message_id),
                    component="workflow",
                    operation="action_build",
                    outcome=EventOutcome.SUCCEEDED,
                    details={"action_status": action.status.value},
                )
            )
        tracker.finish(
            processed_count=len(actions),
            detail=f"added={update.added_count}, skipped={update.skipped_count}",
        )
        return {
            "generated": len(actions),
            "added": update.added_count,
            "skipped": update.skipped_count,
            "audit_added": audit_update.appended_count,
        }

    def _record_analysis_events(
        self,
        tracker: _RunTracker,
        analysis: AnalysisReport,
    ) -> None:
        for result in analysis.results:
            tracker.record(
                ObservabilityEvent(
                    occurred_at=result.evaluated_at,
                    run_id=tracker.run_id,
                    message_hash=safe_message_hash(result.message_id),
                    component="pipeline",
                    operation="message_analysis",
                    outcome=EventOutcome.SUCCEEDED,
                    details={
                        "priority": result.priority.value,
                        "category": result.category,
                        "requires_review": result.requires_review,
                        "decision_source": result.decision_source.value,
                    },
                )
            )
        for failure in analysis.failures:
            tracker.record(
                ObservabilityEvent(
                    occurred_at=self.clock(),
                    run_id=tracker.run_id,
                    message_hash=safe_message_hash(failure.message_id),
                    component="pipeline",
                    operation=failure.stage,
                    outcome=EventOutcome.FAILED,
                    error_type=failure.error_type,
                )
            )
        for llm_result in analysis.llm_analyses:
            usage = llm_result.usage
            tracker.record(
                ObservabilityEvent(
                    occurred_at=llm_result.analyzed_at,
                    run_id=tracker.run_id,
                    message_hash=safe_message_hash(llm_result.message_id),
                    component="llm",
                    operation="llm_call",
                    outcome=EventOutcome.SUCCEEDED,
                    duration_ms=llm_result.duration_ms,
                    provider=llm_result.provider,
                    model_name=llm_result.model_name,
                    input_tokens=usage.input_tokens if usage is not None else None,
                    output_tokens=usage.output_tokens if usage is not None else None,
                    cached_input_tokens=usage.cached_input_tokens if usage is not None else None,
                    estimated_cost_microusd=estimate_llm_cost(
                        self.llm_pricing,
                        provider=llm_result.provider,
                        model_name=llm_result.model_name,
                        usage=usage,
                    ),
                )
            )
        for failure in analysis.llm_failures:
            tracker.record(
                ObservabilityEvent(
                    occurred_at=self.clock(),
                    run_id=tracker.run_id,
                    message_hash=safe_message_hash(failure.message_id),
                    component="llm",
                    operation="llm_call",
                    outcome=EventOutcome.FAILED,
                    provider=(
                        self.llm_provider.provider_name if self.llm_provider is not None else None
                    ),
                    model_name=(
                        self.llm_provider.model_name if self.llm_provider is not None else None
                    ),
                    error_type=failure.error_type,
                )
            )
