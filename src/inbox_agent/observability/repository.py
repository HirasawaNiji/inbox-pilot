"""Persistence and aggregation for privacy-bounded observability events."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from inbox_agent.observability.logging import StructuredLogWriter, sanitize_mapping, sanitize_text
from inbox_agent.observability.models import (
    EventOutcome,
    ObservabilityEvent,
    ObservabilityEventRecord,
    OperationsStatistics,
    ProviderStatistics,
)
from inbox_agent.storage.database import Database
from inbox_agent.storage.orm import (
    ActionRow,
    AnalysisRow,
    NotificationDeliveryRow,
    ObservabilityEventRow,
)


class ObservabilityRecorder:
    """Persist one event to SQLite and mirror it to structured JSONL."""

    def __init__(self, database: Database, *, log_path: Path | None = None) -> None:
        path = database.path.parent / "logs" / "inbox-pilot.jsonl"
        if log_path is not None:
            path = log_path if log_path.is_absolute() else database.path.parent / log_path
        self.database = database
        self.log_writer = StructuredLogWriter(path)

    def record(self, event: ObservabilityEvent) -> int:
        details_json = json.dumps(
            sanitize_mapping(event.details),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.database.session() as session:
            row = ObservabilityEventRow(
                occurred_at=event.occurred_at.isoformat(),
                run_id=event.run_id,
                message_hash=event.message_hash,
                component=event.component,
                operation=event.operation,
                outcome=event.outcome.value,
                duration_ms=event.duration_ms,
                provider=(
                    sanitize_text(event.provider, maximum_length=100) if event.provider else None
                ),
                model_name=(
                    sanitize_text(event.model_name, maximum_length=200)
                    if event.model_name
                    else None
                ),
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cached_input_tokens=event.cached_input_tokens,
                estimated_cost_microusd=event.estimated_cost_microusd,
                error_type=(
                    sanitize_text(event.error_type, maximum_length=100)
                    if event.error_type
                    else None
                ),
                details_json=details_json,
            )
            session.add(row)
            session.flush()
            event_id = row.id
        self.log_writer.write(event)
        return event_id

    def trace_message(self, message_hash: str) -> tuple[ObservabilityEventRecord, ...]:
        with self.database.session() as session:
            rows = session.scalars(
                select(ObservabilityEventRow)
                .where(ObservabilityEventRow.message_hash == message_hash)
                .order_by(ObservabilityEventRow.occurred_at, ObservabilityEventRow.id)
            ).all()
        return tuple(_record(row) for row in rows)

    def statistics(
        self, *, window_hours: int = 24, now: datetime | None = None
    ) -> OperationsStatistics:
        current = now or datetime.now(UTC)
        since = (current - timedelta(hours=window_hours)).isoformat()
        with self.database.session() as session:
            events = list(
                session.scalars(
                    select(ObservabilityEventRow).where(ObservabilityEventRow.occurred_at >= since)
                ).all()
            )
            latest_analysis_ids = (
                select(func.max(AnalysisRow.id).label("analysis_id"))
                .where(AnalysisRow.complete == 1)
                .group_by(AnalysisRow.message_id)
                .subquery()
            )
            review_backlog = (
                session.scalar(
                    select(func.count())
                    .select_from(AnalysisRow)
                    .join(
                        latest_analysis_ids,
                        AnalysisRow.id == latest_analysis_ids.c.analysis_id,
                    )
                    .where(AnalysisRow.requires_review == 1)
                )
                or 0
            )
            action_backlog = (
                session.scalar(
                    select(func.count())
                    .select_from(ActionRow)
                    .where(
                        ActionRow.status.in_(
                            (
                                "pending_review",
                                "approved",
                                "executing",
                                "write_in_flight",
                                "failed",
                                "outcome_unknown",
                                "rollback_executing",
                                "rollback_write_in_flight",
                                "rollback_failed",
                                "rollback_outcome_unknown",
                            )
                        )
                    )
                )
                or 0
            )
            notification_backlog = (
                session.scalar(
                    select(func.count())
                    .select_from(NotificationDeliveryRow)
                    .where(NotificationDeliveryRow.status.in_(("pending", "failed")))
                )
                or 0
            )
        run_events = [event for event in events if event.operation == "workflow_run"]
        successes = sum(event.outcome == EventOutcome.SUCCEEDED.value for event in run_events)
        failed_runs = sum(
            event.outcome in {EventOutcome.FAILED.value, EventOutcome.COMPLETED_WITH_FAILURES.value}
            for event in run_events
        )
        durations = [event.duration_ms for event in run_events if event.duration_ms is not None]
        errors = [event for event in events if event.error_type is not None]
        latest_error = (
            max(errors, key=lambda event: (event.occurred_at, event.id)) if errors else None
        )
        return OperationsStatistics(
            window_hours=window_hours,
            workflow_runs=len(run_events),
            successful_runs=successes,
            failed_runs=failed_runs,
            workflow_success_rate=successes / len(run_events) if run_events else None,
            average_workflow_duration_ms=round(sum(durations) / len(durations))
            if durations
            else None,
            review_backlog=review_backlog,
            action_backlog=action_backlog,
            notification_backlog=notification_backlog,
            latest_error_type=latest_error.error_type if latest_error is not None else None,
            latest_error_at=datetime.fromisoformat(latest_error.occurred_at)
            if latest_error is not None
            else None,
            providers=_provider_statistics(events),
        )


def _record(row: ObservabilityEventRow) -> ObservabilityEventRecord:
    raw_details = json.loads(row.details_json)
    return ObservabilityEventRecord(
        event_id=row.id,
        occurred_at=datetime.fromisoformat(row.occurred_at),
        run_id=row.run_id,
        message_hash=row.message_hash,
        component=row.component,
        operation=row.operation,
        outcome=EventOutcome(row.outcome),
        duration_ms=row.duration_ms,
        provider=row.provider,
        model_name=row.model_name,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cached_input_tokens=row.cached_input_tokens,
        estimated_cost_microusd=row.estimated_cost_microusd,
        error_type=row.error_type,
        details=raw_details if isinstance(raw_details, dict) else {},
    )


def _provider_statistics(rows: list[ObservabilityEventRow]) -> tuple[ProviderStatistics, ...]:
    grouped: dict[tuple[str, str | None], list[ObservabilityEventRow]] = defaultdict(list)
    for row in rows:
        if row.operation == "llm_call" and row.provider is not None:
            grouped[(row.provider, row.model_name)].append(row)
    results = []
    for (provider, model_name), events in sorted(grouped.items()):
        successes = sum(event.outcome == EventOutcome.SUCCEEDED.value for event in events)
        failures = sum(event.outcome == EventOutcome.FAILED.value for event in events)
        known_costs = [
            event.estimated_cost_microusd
            for event in events
            if event.estimated_cost_microusd is not None
        ]
        results.append(
            ProviderStatistics(
                provider=provider,
                model_name=model_name,
                attempts=len(events),
                successes=successes,
                failures=failures,
                success_rate=successes / len(events) if events else None,
                input_tokens=sum(event.input_tokens or 0 for event in events),
                output_tokens=sum(event.output_tokens or 0 for event in events),
                cached_input_tokens=sum(event.cached_input_tokens or 0 for event in events),
                total_duration_ms=sum(event.duration_ms or 0 for event in events),
                estimated_cost_usd=sum(known_costs) / 1_000_000
                if len(known_costs) == len(events)
                else None,
            )
        )
    return tuple(results)
