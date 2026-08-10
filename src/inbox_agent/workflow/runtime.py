"""Application-layer construction for one complete workflow run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from inbox_agent.graph import (
    GraphInboxSynchronizer,
    GraphMailClient,
    GraphTokenProvider,
    load_graph_settings,
)
from inbox_agent.llm import LLMProvider, OpenAICompatibleProvider
from inbox_agent.observability import LLMPricingRate, ObservabilityRecorder
from inbox_agent.pipeline import OfflinePipeline
from inbox_agent.storage import Database, upgrade_database
from inbox_agent.workflow.models import DatasetSyncResult, WorkflowReport
from inbox_agent.workflow.orchestrator import WorkflowOrchestrator, build_analysis_profile


def _resolved(project_root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


@dataclass(frozen=True, slots=True)
class WorkflowRuntimeSettings:
    """Paths and feature switches required to construct one workflow safely."""

    project_root: Path
    dataset_path: Path
    database_path: Path
    action_queue_path: Path
    audit_log_path: Path
    policy_path: Path
    llm_config_path: Path | None = None
    llm_routing_path: Path | None = None
    llm_fusion_path: Path | None = None
    sync_outlook: bool = False
    graph_config_path: Path | None = None
    observability_enabled: bool = True
    observability_log_path: Path | None = None
    llm_pricing: tuple[LLMPricingRate, ...] = ()

    def resolved(self) -> WorkflowRuntimeSettings:
        """Resolve every relative path against the project root, never the process cwd."""

        root = self.project_root.resolve()
        return WorkflowRuntimeSettings(
            project_root=root,
            dataset_path=_resolved(root, self.dataset_path),
            database_path=_resolved(root, self.database_path),
            action_queue_path=_resolved(root, self.action_queue_path),
            audit_log_path=_resolved(root, self.audit_log_path),
            policy_path=_resolved(root, self.policy_path),
            llm_config_path=(
                _resolved(root, self.llm_config_path) if self.llm_config_path is not None else None
            ),
            llm_routing_path=(
                _resolved(root, self.llm_routing_path)
                if self.llm_routing_path is not None
                else None
            ),
            llm_fusion_path=(
                _resolved(root, self.llm_fusion_path) if self.llm_fusion_path is not None else None
            ),
            sync_outlook=self.sync_outlook,
            graph_config_path=(
                _resolved(root, self.graph_config_path)
                if self.graph_config_path is not None
                else None
            ),
            observability_enabled=self.observability_enabled,
            observability_log_path=(
                _resolved(root, self.observability_log_path)
                if self.observability_log_path is not None
                else None
            ),
            llm_pricing=self.llm_pricing,
        )


def execute_workflow(
    settings: WorkflowRuntimeSettings,
    *,
    force: bool = False,
    llm_provider: LLMProvider | None = None,
) -> WorkflowReport:
    """Build dependencies, execute one workflow, and always dispose SQLite resources."""

    configured = settings.resolved()
    provider = (
        llm_provider
        if llm_provider is not None
        else OpenAICompatibleProvider.from_yaml(configured.llm_config_path)
        if configured.llm_config_path is not None
        else None
    )
    pipeline = OfflinePipeline.from_yaml(
        configured.policy_path,
        llm_provider=provider,
        llm_routing_path=configured.llm_routing_path if provider is not None else None,
        llm_fusion_path=configured.llm_fusion_path if provider is not None else None,
    )
    profile = build_analysis_profile(
        configured.policy_path,
        llm_provider=provider,
        llm_routing_path=configured.llm_routing_path if provider is not None else None,
        llm_fusion_path=configured.llm_fusion_path if provider is not None else None,
    )
    upgrade_database(configured.database_path)
    database = Database(configured.database_path)
    try:
        observability = (
            ObservabilityRecorder(
                database,
                log_path=configured.observability_log_path,
            )
            if configured.observability_enabled
            else None
        )
        dataset_sync = None
        if configured.sync_outlook:
            if configured.graph_config_path is None:
                raise ValueError("graph_config_path is required when sync_outlook is enabled")
            graph_settings = load_graph_settings(configured.graph_config_path)

            def synchronize_outlook() -> DatasetSyncResult:
                graph_provider = GraphTokenProvider.from_settings(
                    graph_settings,
                    configured.project_root,
                )
                token = graph_provider.acquire_silent()
                with httpx.Client() as http_client:
                    client = GraphMailClient(graph_settings, http_client)
                    sync_report = GraphInboxSynchronizer(
                        graph_settings,
                        client,
                        configured.project_root,
                    ).sync(token)
                return DatasetSyncResult(
                    dataset_path=_resolved(
                        configured.project_root,
                        sync_report.dataset_path,
                    ),
                    completed=sync_report.completed,
                    created_count=sync_report.created_count,
                    updated_count=sync_report.updated_count,
                    removed_count=sync_report.removed_count,
                    unchanged_count=sync_report.unchanged_count,
                    failure_count=len(sync_report.failures),
                )

            dataset_sync = synchronize_outlook

        return WorkflowOrchestrator(
            database=database,
            pipeline=pipeline,
            analysis_profile=profile,
            action_queue_path=configured.action_queue_path,
            audit_log_path=configured.audit_log_path,
            llm_provider=provider,
            observability=observability,
            llm_pricing=configured.llm_pricing,
        ).run(configured.dataset_path, force=force, dataset_sync=dataset_sync)
    finally:
        database.dispose()
