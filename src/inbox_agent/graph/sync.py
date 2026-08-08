"""Private, atomic Outlook Inbox delta synchronization."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from pydantic import Field, ValidationError

from inbox_agent.graph.auth import GraphAccessToken
from inbox_agent.graph.client import GRAPH_MESSAGE_ID_TYPE, GraphMailClient
from inbox_agent.graph.config import GraphSettings
from inbox_agent.graph.mapper import map_graph_message
from inbox_agent.loader import DatasetLoadError, load_dataset
from inbox_agent.models import EmailMessage, FrozenModel, MessageDataset

GRAPH_MESSAGE_FIELDS = (
    "id",
    "internetMessageId",
    "subject",
    "from",
    "sender",
    "replyTo",
    "toRecipients",
    "ccRecipients",
    "receivedDateTime",
    "sentDateTime",
    "body",
    "bodyPreview",
    "importance",
    "inferenceClassification",
    "categories",
    "changeKey",
    "hasAttachments",
)
GRAPH_SYNC_QUERY_CONTRACT_VERSION = "2.0"


class GraphSyncState(FrozenModel):
    """Opaque delta checkpoint that never leaves private local storage."""

    schema_version: str = "1.0"
    query_contract_version: str = Field(
        default=GRAPH_SYNC_QUERY_CONTRACT_VERSION,
        pattern=r"^2[.]0$",
    )
    message_id_type: str = Field(default=GRAPH_MESSAGE_ID_TYPE, pattern=r"^restImmutableEntryId$")
    mail_folder: str = Field(pattern=r"^inbox$")
    delta_link: str = Field(min_length=1, max_length=20_000)
    synchronized_at: datetime


class GraphSyncFailure(FrozenModel):
    """One Graph message that could not map into the local data contract."""

    message_id: str = Field(min_length=1, max_length=512)
    error_type: str = Field(min_length=1, max_length=100)
    error_message: str = Field(min_length=1, max_length=500)


class GraphSyncReport(FrozenModel):
    """Auditable counts for one complete or partially mapped delta round."""

    started_from_delta: bool
    message_id_type: str = Field(default=GRAPH_MESSAGE_ID_TYPE, pattern=r"^restImmutableEntryId$")
    completed: bool
    pages_fetched: int = Field(ge=0)
    created_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    removed_count: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    total_messages: int = Field(ge=0)
    failures: tuple[GraphSyncFailure, ...] = ()
    dataset_path: Path
    state_path: Path


class GraphSyncStorageError(Exception):
    """Raised when private sync state or dataset storage is invalid."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _private_path(project_root: Path, relative_path: Path) -> Path:
    return project_root / relative_path


def _read_state(path: Path) -> GraphSyncState | None:
    try:
        raw_content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise GraphSyncStorageError(f"Unable to read private Graph state: {path}") from error
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError as error:
        raise GraphSyncStorageError(f"Private Graph state is invalid: {path}") from error
    if not isinstance(payload, dict):
        raise GraphSyncStorageError(f"Private Graph state is invalid: {path}")

    # Delta links retain the query options from their original request. States
    # created before the write-safety fields were selected cannot supply a
    # category snapshot or changeKey, so force one fresh read-only round.
    if payload.get("query_contract_version") != GRAPH_SYNC_QUERY_CONTRACT_VERSION:
        return None
    try:
        return GraphSyncState.model_validate(payload)
    except ValidationError as error:
        raise GraphSyncStorageError(f"Private Graph state is invalid: {path}") from error


def _read_dataset(path: Path) -> MessageDataset:
    if not path.exists():
        return MessageDataset(messages=())
    try:
        return load_dataset(path)
    except DatasetLoadError as error:
        raise GraphSyncStorageError(f"Private Outlook dataset is invalid: {path}") from error


def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except OSError as error:
        raise GraphSyncStorageError(f"Unable to write private Graph data: {path}") from error
    finally:
        temporary_path.unlink(missing_ok=True)


def _initial_delta_url(settings: GraphSettings, now: datetime) -> str:
    cutoff = (now.astimezone(UTC) - timedelta(days=settings.initial_sync_days)).isoformat()
    query = urlencode(
        {
            "$select": ",".join(GRAPH_MESSAGE_FIELDS),
            "$filter": f"receivedDateTime ge {cutoff}",
            "$orderby": "receivedDateTime desc",
        }
    )
    return (
        "https://graph.microsoft.com/v1.0/me/mailFolders/"
        f"{settings.mail_folder}/messages/delta?{query}"
    )


def _message_id(payload: Mapping[str, object]) -> str:
    value = payload.get("id")
    return value if isinstance(value, str) and value else "unknown-graph-message"


def _is_removed(payload: Mapping[str, object]) -> bool:
    removed = payload.get("@removed")
    return isinstance(removed, Mapping)


class GraphInboxSynchronizer:
    """Apply one folder-scoped delta round to a private MessageDataset."""

    def __init__(
        self,
        settings: GraphSettings,
        client: GraphMailClient,
        project_root: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.settings = settings
        self.client = client
        self.project_root = project_root
        self.clock = clock

    def sync(self, token: GraphAccessToken) -> GraphSyncReport:
        """Fetch all pages, update private data, and checkpoint only a clean round."""

        run_time = self.clock()
        state_path = _private_path(self.project_root, self.settings.sync_state_path)
        dataset_path = _private_path(self.project_root, self.settings.dataset_path)
        state = _read_state(state_path)
        dataset = _read_dataset(dataset_path)
        messages: dict[str, EmailMessage] = {
            message.source_id: message for message in dataset.messages
        }

        url: str | None = (
            state.delta_link if state is not None else _initial_delta_url(self.settings, run_time)
        )
        final_delta_link: str | None = None
        pages_fetched = 0
        created_count = 0
        updated_count = 0
        removed_count = 0
        unchanged_count = 0
        failures: list[GraphSyncFailure] = []

        while url is not None:
            page = self.client.get_delta_page(url, token)
            pages_fetched += 1
            for payload in page.values:
                message_id = _message_id(payload)
                if _is_removed(payload):
                    if messages.pop(message_id, None) is not None:
                        removed_count += 1
                    else:
                        unchanged_count += 1
                    continue
                try:
                    message = map_graph_message(payload)
                except (ValidationError, ValueError) as error:
                    failures.append(
                        GraphSyncFailure(
                            message_id=message_id,
                            error_type=type(error).__name__,
                            error_message=str(error).replace("\r", " ").replace("\n", " ")[:500],
                        )
                    )
                    continue
                previous = messages.get(message.source_id)
                if previous is None:
                    created_count += 1
                elif previous == message:
                    unchanged_count += 1
                else:
                    updated_count += 1
                messages[message.source_id] = message

            url = page.next_link
            if page.delta_link is not None:
                final_delta_link = page.delta_link

        synchronized_dataset = MessageDataset(
            messages=tuple(
                sorted(messages.values(), key=lambda item: (item.received_at, item.source_id))
            )
        )
        _atomic_write(dataset_path, synchronized_dataset.model_dump(mode="json"))

        completed = not failures and final_delta_link is not None
        if completed:
            assert final_delta_link is not None
            next_state = GraphSyncState(
                mail_folder=self.settings.mail_folder,
                delta_link=final_delta_link,
                synchronized_at=run_time,
            )
            _atomic_write(state_path, next_state.model_dump(mode="json"))

        return GraphSyncReport(
            started_from_delta=state is not None,
            completed=completed,
            pages_fetched=pages_fetched,
            created_count=created_count,
            updated_count=updated_count,
            removed_count=removed_count,
            unchanged_count=unchanged_count,
            total_messages=len(synchronized_dataset.messages),
            failures=tuple(failures),
            dataset_path=self.settings.dataset_path,
            state_path=self.settings.sync_state_path,
        )
