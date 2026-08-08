"""Tests for cross-process protection of private queue and audit files."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from inbox_agent.actions import (
    ActionAuditLog,
    ActionFileLock,
    ActionFileLockTimeoutError,
    ActionQueueRepository,
    ActionQueueStorageError,
    MailboxAction,
    audit_events_for_action,
    build_review_actions,
)
from inbox_agent.loader import load_dataset
from inbox_agent.pipeline import OfflinePipeline

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "rules.yaml"
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
CREATED_AT = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)


def make_actions(count: int = 2) -> tuple[MailboxAction, ...]:
    dataset = load_dataset(DATASET_PATH)
    dataset = dataset.model_copy(update={"messages": dataset.messages[:count]})
    analysis = OfflinePipeline.from_yaml(POLICY_PATH).analyze_dataset(
        dataset,
        evaluated_at=CREATED_AT,
    )
    return build_review_actions(dataset, analysis)


def test_file_lock_times_out_then_can_be_reacquired(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"
    first = ActionFileLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(ActionFileLockTimeoutError, match="Timed out"):
            ActionFileLock(lock_path, timeout_seconds=0).acquire()
    finally:
        first.release()

    with ActionFileLock(lock_path, timeout_seconds=0):
        assert lock_path.exists()


def test_queue_wraps_lock_timeout_as_storage_error(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    repository = ActionQueueRepository(queue_path, lock_timeout_seconds=0)

    with ActionFileLock(repository.lock_path):
        with pytest.raises(ActionQueueStorageError, match="Unable to lock"):
            repository.load()


def test_concurrent_queue_updates_do_not_lose_actions(tmp_path: Path) -> None:
    actions = make_actions()
    queue_path = tmp_path / "queue.json"
    barrier = Barrier(2)

    def enqueue_one(index: int) -> None:
        barrier.wait()
        ActionQueueRepository(queue_path).enqueue((actions[index],))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(enqueue_one, index) for index in range(2)]
        for future in futures:
            future.result()

    loaded = ActionQueueRepository(queue_path).load()
    assert {action.action_id for action in loaded.actions} == {
        action.action_id for action in actions
    }


def test_concurrent_audit_appends_do_not_lose_events(tmp_path: Path) -> None:
    actions = make_actions()
    log_path = tmp_path / "actions.jsonl"
    barrier = Barrier(2)

    def append_one(index: int) -> None:
        barrier.wait()
        ActionAuditLog(log_path).append_unique(audit_events_for_action(actions[index]))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(append_one, index) for index in range(2)]
        for future in futures:
            future.result()

    assert len(ActionAuditLog(log_path).load()) == 2
