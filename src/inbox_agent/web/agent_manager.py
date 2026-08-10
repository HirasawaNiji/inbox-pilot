"""In-process management for scheduled sync and transient LLM credentials."""

from __future__ import annotations

from threading import RLock, Thread

from pydantic import Field

from inbox_agent.llm import (
    LLMProvider,
    OpenAICompatibleProvider,
    OpenAICompatibleService,
    OpenAICompatibleSettings,
)
from inbox_agent.models import FrozenModel
from inbox_agent.notifications import NotificationCoordinator
from inbox_agent.observability import ObservabilityRecorder
from inbox_agent.service import (
    ServiceConfigurationError,
    ServiceRunner,
    inspect_service,
    load_service_settings,
)
from inbox_agent.storage import Database, upgrade_database
from inbox_agent.web.config import WebSettings
from inbox_agent.workflow import execute_workflow

WEB_LLM_KEY_NAME = "INBOX_PILOT_WEB_LLM_API_KEY"
PROVIDER_BASE_URLS = {
    OpenAICompatibleService.OPENAI: "https://api.openai.com/v1",
    OpenAICompatibleService.DEEPSEEK: "https://api.deepseek.com",
}
PROVIDER_MODELS = {
    OpenAICompatibleService.OPENAI: (
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ),
    OpenAICompatibleService.DEEPSEEK: (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ),
}


class WebAgentError(RuntimeError):
    """Privacy-safe error raised by Web-managed background operations."""

    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


class ManagedAgentStatus(FrozenModel):
    """Browser-safe snapshot that never contains an API key."""

    sync_owned: bool = False
    sync_active: bool = False
    sync_external: bool = False
    sync_stopping: bool = False
    service_configured: bool = False
    interval_minutes: int | None = Field(default=None, ge=1)
    llm_enabled: bool = False
    llm_provider: OpenAICompatibleService | None = None
    llm_model: str | None = None
    api_key_loaded: bool = False
    last_error: str | None = Field(default=None, max_length=200)


class WebAgentManager:
    """Own one optional scheduler thread and memory-only LLM configuration."""

    def __init__(self, settings: WebSettings) -> None:
        self.settings = settings.resolved()
        self._lock = RLock()
        self._runner: ServiceRunner | None = None
        self._thread: Thread | None = None
        self._database: Database | None = None
        self._stopping = False
        self._last_error: str | None = None
        self._llm_settings: OpenAICompatibleSettings | None = None
        self._api_key: str | None = None

    def status(self) -> ManagedAgentStatus:
        """Combine managed-thread state with the existing OS-lock probe."""

        with self._lock:
            owned = self._thread is not None and self._thread.is_alive()
            stopping = self._stopping
            llm_settings = self._llm_settings
            key_loaded = bool(self._api_key)
            last_error = self._last_error
        try:
            service_settings = load_service_settings(self.settings.service_config_path)
            report = inspect_service(
                service_settings,
                config_path=self.settings.service_config_path,
                project_root=self.settings.project_root,
            )
        except ServiceConfigurationError:
            return ManagedAgentStatus(
                sync_owned=owned,
                sync_active=owned,
                sync_stopping=stopping,
                llm_enabled=llm_settings is not None and key_loaded,
                llm_provider=(llm_settings.provider if llm_settings is not None else None),
                llm_model=(llm_settings.model if llm_settings is not None else None),
                api_key_loaded=key_loaded,
                last_error=last_error,
            )
        active = report.active or owned
        return ManagedAgentStatus(
            sync_owned=owned,
            sync_active=active,
            sync_external=report.active and not owned,
            sync_stopping=stopping,
            service_configured=True,
            interval_minutes=service_settings.interval_minutes,
            llm_enabled=llm_settings is not None and key_loaded,
            llm_provider=(llm_settings.provider if llm_settings is not None else None),
            llm_model=(llm_settings.model if llm_settings is not None else None),
            api_key_loaded=key_loaded,
            last_error=last_error,
        )

    def configure_llm(
        self,
        *,
        enabled: bool,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> ManagedAgentStatus:
        """Replace transient LLM settings; disabled remains the startup default."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise WebAgentError(
                    "SYNC_ACTIVE",
                    "Stop managed synchronization before changing LLM settings",
                )
            if not enabled:
                self._llm_settings = None
                self._api_key = None
                return self.status()
            normalized_key = (api_key or "").strip()
            if (
                not normalized_key
                or len(normalized_key) > 10_000
                or any(character.isspace() for character in normalized_key)
            ):
                raise WebAgentError("INVALID_LLM_KEY", "A valid API key is required")
            try:
                service = OpenAICompatibleService(provider or "")
                normalized_model = (model or "").strip()
                if normalized_model not in PROVIDER_MODELS[service]:
                    raise ValueError("model is not supported by the selected provider")
                settings = OpenAICompatibleSettings(
                    provider=service,
                    model=normalized_model,
                    base_url=PROVIDER_BASE_URLS[service],
                    api_key_env=WEB_LLM_KEY_NAME,
                )
            except (KeyError, ValueError) as error:
                raise WebAgentError(
                    "INVALID_LLM_SETTINGS",
                    "The selected LLM provider or model is invalid",
                ) from error
            self._llm_settings = settings
            self._api_key = normalized_key
            self._last_error = None
            return self.status()

    def start_sync(self) -> ManagedAgentStatus:
        """Start one scheduler thread using existing locks and workflow services."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise WebAgentError("SYNC_ALREADY_RUNNING", "Managed synchronization is running")
            try:
                service_settings = load_service_settings(self.settings.service_config_path)
                report = inspect_service(
                    service_settings,
                    config_path=self.settings.service_config_path,
                    project_root=self.settings.project_root,
                )
            except ServiceConfigurationError as error:
                raise WebAgentError(
                    "SERVICE_CONFIGURATION_UNAVAILABLE",
                    "The local scheduler configuration is unavailable",
                ) from error
            if report.active:
                raise WebAgentError(
                    "EXTERNAL_SYNC_ACTIVE",
                    "Another synchronization service already holds the scheduler lock",
                )

            workflow_settings = service_settings.workflow.model_copy(
                update={"llm_config_path": None}
            )
            effective_settings = service_settings.model_copy(update={"workflow": workflow_settings})
            runtime = effective_settings.runtime_settings(self.settings.project_root)
            provider = self._build_provider()
            upgrade_database(runtime.database_path)
            database = Database(runtime.database_path)
            notifications = NotificationCoordinator(
                database=database,
                action_queue_path=runtime.action_queue_path,
                output_dir=service_settings.notifications.resolved_output_dir(
                    self.settings.project_root
                ),
                settings=service_settings.notifications,
            )
            runner = ServiceRunner(
                settings=effective_settings,
                database=database,
                lock_path=service_settings.resolved_lock_path(self.settings.project_root),
                execute_workflow=lambda: execute_workflow(
                    runtime,
                    llm_provider=provider,
                ),
                result_processor=notifications.process,
                observability=(
                    ObservabilityRecorder(database, log_path=runtime.observability_log_path)
                    if runtime.observability_enabled
                    else None
                ),
            )
            thread = Thread(
                target=self._serve,
                args=(runner, database),
                name="inbox-pilot-sync",
                daemon=False,
            )
            self._runner = runner
            self._database = database
            self._thread = thread
            self._stopping = False
            self._last_error = None
            thread.start()
        return self.status()

    def stop_sync(self) -> ManagedAgentStatus:
        """Request a managed scheduler stop; an active workflow may finish first."""

        with self._lock:
            thread = self._thread
            runner = self._runner
            if thread is None or not thread.is_alive() or runner is None:
                current = self.status()
                if current.sync_external:
                    raise WebAgentError(
                        "EXTERNAL_SYNC_ACTIVE",
                        "Stop the external synchronization service in its original terminal",
                    )
                raise WebAgentError("SYNC_NOT_RUNNING", "Managed synchronization is not running")
            self._stopping = True
            runner.request_stop()
        return self.status()

    def shutdown(self) -> None:
        """Stop the scheduler gracefully and remove all in-memory credentials."""

        with self._lock:
            runner = self._runner
            thread = self._thread
            if runner is not None and thread is not None and thread.is_alive():
                self._stopping = True
                runner.request_stop()
        if thread is not None and thread.is_alive():
            thread.join()
        with self._lock:
            self._llm_settings = None
            self._api_key = None

    def _build_provider(self) -> LLMProvider | None:
        settings = self._llm_settings
        api_key = self._api_key
        if settings is None or api_key is None:
            return None
        return OpenAICompatibleProvider.from_settings(settings, api_key)

    def _serve(self, runner: ServiceRunner, database: Database) -> None:
        try:
            runner.serve()
        except Exception as error:  # noqa: BLE001 - preserve Web process and safe state
            with self._lock:
                self._last_error = f"{type(error).__name__}: synchronization stopped"
        finally:
            database.dispose()
            with self._lock:
                if self._runner is runner:
                    self._runner = None
                    self._database = None
                    self._thread = None
                    self._stopping = False
