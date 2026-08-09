"""Command-line integration tests for InboxPilot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from inbox_agent.actions import (
    ActionGraphExecutionOutcome,
    ActionGraphExecutionReport,
    ActionQueueRepository,
    ActionReconciliationOutcome,
    ActionReconciliationReport,
    MailboxActionStatus,
)
from inbox_agent.cli import app
from inbox_agent.graph import GraphAccessToken, GraphSyncReport
from inbox_agent.llm import FakeLLMProvider, OpenAICompatibleProvider
from inbox_agent.models import LLMMessageAnalysis, MessageCategory, Priority

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
POLICY_PATH = ROOT / "config" / "rules.yaml"
runner = CliRunner()
GRAPH_CLIENT_ID = "12345678-1234-4234-8234-123456789abc"


def write_graph_config(tmp_path: Path) -> Path:
    path = tmp_path / "graph.local.yaml"
    path.write_text(
        f"client_id: {GRAPH_CLIENT_ID}\naccount_audience: consumers\nscopes: [Mail.Read]\n",
        encoding="utf-8",
    )
    return path


def write_graph_write_config(tmp_path: Path, *, enabled: bool) -> Path:
    path = tmp_path / "graph_write.local.yaml"
    path.write_text(
        f"client_id: {GRAPH_CLIENT_ID}\n"
        "account_audience: consumers\n"
        "scopes: [Mail.ReadWrite]\n"
        f"write_enabled: {str(enabled).lower()}\n",
        encoding="utf-8",
    )
    return path


def write_service_config(tmp_path: Path) -> Path:
    path = tmp_path / "service.local.yaml"
    path.write_text(
        "schema_version: '1.0'\n"
        "service_name: cli-test\n"
        "interval_minutes: 1\n"
        "max_backoff_minutes: 2\n"
        "run_immediately: true\n"
        f"lock_path: {(tmp_path / 'private/service.lock').as_posix()}\n"
        "workflow:\n"
        f"  dataset_path: {DATASET_PATH.as_posix()}\n"
        f"  database_path: {(tmp_path / 'private/service.sqlite3').as_posix()}\n"
        f"  action_queue_path: {(tmp_path / 'private/service-actions.json').as_posix()}\n"
        f"  audit_log_path: {(tmp_path / 'private/service-audit.jsonl').as_posix()}\n"
        f"  policy_path: {POLICY_PATH.as_posix()}\n"
        "  sync_outlook: false\n",
        encoding="utf-8",
    )
    return path


def test_demo_runs_bundled_dataset() -> None:
    result = runner.invoke(app, ["demo"])

    assert result.exit_code == 0
    assert "InboxPilot Analysis" in result.stdout
    assert "成功 50" in result.stdout
    assert "待复核 7" in result.stdout
    assert "sample-" not in result.stdout


def test_database_status_does_not_create_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "missing.sqlite3"

    result = runner.invoke(
        app,
        ["db", "status", "--database", str(database_path), "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["initialized"] is False
    assert payload["revision"] is None
    assert payload["counts"]["messages"] == 0
    assert not database_path.exists()


def test_database_init_is_idempotent_and_reports_revision(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox_pilot.sqlite3"

    first = runner.invoke(
        app,
        ["db", "init", "--database", str(database_path), "--format", "json"],
    )
    second = runner.invoke(
        app,
        ["db", "init", "--database", str(database_path), "--format", "json"],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    payload = json.loads(second.stdout)
    assert payload["initialized"] is True
    assert payload["revision"] == "0003_service"
    assert payload["counts"] == {
        "messages": 0,
        "analyses": 0,
        "actions": 0,
        "sync_cursors": 0,
        "workflow_runs": 0,
    }


def test_database_import_json_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox_pilot.sqlite3"
    command = [
        "db",
        "import-json",
        str(DATASET_PATH),
        "--database",
        str(database_path),
        "--format",
        "json",
    ]

    first = runner.invoke(app, command)
    second = runner.invoke(app, command)
    status = runner.invoke(
        app,
        ["db", "status", "--database", str(database_path), "--format", "json"],
    )

    assert first.exit_code == 0
    assert json.loads(first.stdout)["created"] == 50
    assert second.exit_code == 0
    second_payload = json.loads(second.stdout)
    assert second_payload["created"] == 0
    assert second_payload["updated"] == 0
    assert second_payload["unchanged"] == 50
    assert second_payload["normalized"] == 50
    assert json.loads(status.stdout)["counts"]["messages"] == 50


def test_workflow_run_and_status_are_incremental(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "inbox_pilot.sqlite3"
    queue_path = tmp_path / "private" / "action_queue.json"
    audit_path = tmp_path / "private" / "audit" / "actions.jsonl"
    command = [
        "workflow",
        "run",
        "--dataset",
        str(DATASET_PATH),
        "--database",
        str(database_path),
        "--queue",
        str(queue_path),
        "--audit-log",
        str(audit_path),
        "--format",
        "json",
    ]

    first = runner.invoke(app, command)
    second = runner.invoke(app, command)
    status = runner.invoke(
        app,
        ["workflow", "status", "--database", str(database_path), "--format", "json"],
    )

    assert first.exit_code == 0
    first_payload = json.loads(first.stdout)
    assert first_payload["eligible_messages"] == 50
    assert first_payload["actions_added"] == 50
    assert first_payload["graph_write_request_count"] == 0
    assert second.exit_code == 0
    second_payload = json.loads(second.stdout)
    assert second_payload["eligible_messages"] == 0
    assert second_payload["skipped_current"] == 50
    assert second_payload["actions_generated"] == 0
    assert status.exit_code == 0
    status_payload = json.loads(status.stdout)
    assert status_payload["latest_run"]["status"] == "completed"
    assert status_payload["latest_run"]["counters"]["graph_write_request_count"] == 0


def test_service_run_once_start_and_status_use_same_incremental_workflow(
    tmp_path: Path,
) -> None:
    config_path = write_service_config(tmp_path)

    first = runner.invoke(
        app,
        ["service", "run-once", "--config", str(config_path), "--format", "json"],
    )
    scheduled = runner.invoke(
        app,
        [
            "service",
            "start",
            "--config",
            str(config_path),
            "--max-runs",
            "1",
            "--format",
            "json",
        ],
    )
    status = runner.invoke(
        app,
        ["service", "status", "--config", str(config_path), "--format", "json"],
    )

    assert first.exit_code == 0
    first_payload = json.loads(first.stdout)
    assert first_payload["outcome"] == "succeeded"
    assert first_payload["workflow_report"]["analyzed_messages"] == 50
    assert first_payload["workflow_report"]["graph_write_request_count"] == 0
    assert scheduled.exit_code == 0
    scheduled_payload = json.loads(scheduled.stdout)
    assert scheduled_payload["workflow_report"]["analyzed_messages"] == 0
    assert scheduled_payload["workflow_report"]["skipped_current"] == 50
    assert status.exit_code == 0
    status_payload = json.loads(status.stdout)
    assert status_payload["active"] is False
    assert status_payload["persisted_status"] == "stopped"
    assert status_payload["database_revision"] == "0003_service"
    assert status_payload["last_run_id"] == scheduled_payload["workflow_report"]["run_id"]


def test_demo_json_is_machine_readable() -> None:
    result = runner.invoke(app, ["demo", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["policy_version"] == "rules-v1"
    assert len(payload["results"]) == 50
    assert payload["failures"] == []


def test_analyze_accepts_explicit_dataset_and_policy() -> None:
    result = runner.invoke(
        app,
        [
            "analyze",
            str(DATASET_PATH),
            "--config",
            str(POLICY_PATH),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["results"]) == 50


def test_show_reasons_displays_score_contributions() -> None:
    result = runner.invoke(app, ["demo", "--show-reasons"])

    assert result.exit_code == 0
    assert "trusted_sender" in result.stdout
    assert "deadline_within_two_days" in result.stdout
    assert "+15" in result.stdout


def test_missing_dataset_returns_clear_error(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"

    result = runner.invoke(app, ["analyze", str(missing_path)])

    assert result.exit_code == 1
    assert "Error:" in result.stderr
    assert "does not exist" in result.stderr
    assert str(missing_path) in result.stderr


def test_missing_policy_returns_clear_error(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.yaml"

    result = runner.invoke(
        app,
        ["analyze", str(DATASET_PATH), "--config", str(missing_path)],
    )

    assert result.exit_code == 1
    assert "Error:" in result.stderr
    assert "does not exist" in result.stderr


def test_missing_llm_provider_config_returns_clear_error(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing-provider.yaml"

    result = runner.invoke(
        app,
        ["analyze", str(DATASET_PATH), "--llm-config", str(missing_path)],
    )

    assert result.exit_code == 1
    assert "Error:" in result.stderr
    assert "LLM provider settings file does not exist" in result.stderr
    assert str(missing_path) in result.stderr


def test_outlook_login_reports_missing_local_config(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["outlook", "login", "--config", str(tmp_path / "missing.yaml")],
    )

    assert result.exit_code == 1
    assert "Graph settings file does not exist" in result.stderr


def test_outlook_login_uses_delegated_provider_without_printing_token(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class FakeProvider:
        @classmethod
        def from_settings(cls, settings: object, project_root: Path) -> FakeProvider:
            return cls()

        def login(self, display_message: object) -> GraphAccessToken:
            return GraphAccessToken("secret-token", "student@outlook.com")

    monkeypatch.setattr("inbox_agent.cli.GraphTokenProvider", FakeProvider)

    result = runner.invoke(
        app,
        ["outlook", "login", "--config", str(write_graph_config(tmp_path))],
    )

    assert result.exit_code == 0
    assert "student@outlook.com" in result.stdout
    assert "Mail.Read (read-only)" in result.stdout
    assert "secret-token" not in result.stdout


def test_outlook_write_login_stops_before_provider_when_disabled(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class ForbiddenProvider:
        @classmethod
        def from_settings(cls, settings: object, project_root: Path) -> ForbiddenProvider:
            raise AssertionError("provider must not be created while write access is disabled")

    monkeypatch.setattr("inbox_agent.cli.GraphTokenProvider", ForbiddenProvider)

    result = runner.invoke(
        app,
        [
            "outlook",
            "write-login",
            "--config",
            str(write_graph_write_config(tmp_path, enabled=False)),
        ],
    )

    assert result.exit_code == 1
    assert "write_enabled: true" in result.stderr


def test_outlook_write_login_authorizes_without_mailbox_write(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class FakeProvider:
        @classmethod
        def from_settings(cls, settings: object, project_root: Path) -> FakeProvider:
            return cls()

        def login(self, display_message: object) -> GraphAccessToken:
            return GraphAccessToken("write-secret-token", "student@outlook.com")

    monkeypatch.setattr("inbox_agent.cli.GraphTokenProvider", FakeProvider)

    result = runner.invoke(
        app,
        [
            "outlook",
            "write-login",
            "--config",
            str(write_graph_write_config(tmp_path, enabled=True)),
        ],
    )

    assert result.exit_code == 0
    assert "Mail.ReadWrite (delegated)" in result.stdout
    assert "No Microsoft Graph mailbox write request was sent" in result.stdout
    assert "write-secret-token" not in result.stdout


def test_outlook_sync_outputs_machine_readable_private_report(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    class FakeProvider:
        @classmethod
        def from_settings(cls, settings: object, project_root: Path) -> FakeProvider:
            return cls()

        def acquire_silent(self) -> GraphAccessToken:
            return GraphAccessToken("secret-token")

    class FakeSynchronizer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def sync(self, token: GraphAccessToken) -> GraphSyncReport:
            return GraphSyncReport(
                started_from_delta=False,
                completed=True,
                pages_fetched=1,
                created_count=2,
                updated_count=0,
                removed_count=0,
                unchanged_count=0,
                total_messages=2,
                dataset_path=Path("data/private/outlook_inbox.json"),
                state_path=Path("data/private/graph_sync_state.json"),
            )

    monkeypatch.setattr("inbox_agent.cli.GraphTokenProvider", FakeProvider)
    monkeypatch.setattr("inbox_agent.cli.GraphInboxSynchronizer", FakeSynchronizer)

    result = runner.invoke(
        app,
        [
            "outlook",
            "sync",
            "--config",
            str(write_graph_config(tmp_path)),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["completed"] is True
    assert payload["created_count"] == 2
    assert payload["dataset_path"].replace("\\", "/") == "data/private/outlook_inbox.json"


def test_invalid_output_format_is_rejected() -> None:
    result = runner.invoke(app, ["demo", "--format", "xml"])

    assert result.exit_code == 2
    assert "Invalid value" in result.stderr


def test_evaluate_reports_perfect_sample_metrics() -> None:
    result = runner.invoke(app, ["evaluate"])

    assert result.exit_code == 0
    assert "优先级准确率" in result.stdout
    assert "100.00%" in result.stdout
    assert "PASS" in result.stdout


def test_evaluate_json_is_machine_readable() -> None:
    result = runner.invoke(app, ["evaluate", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["passed"] is True
    assert payload["priority_accuracy"] == 1.0
    assert payload["p1_recall"] == 1.0


def test_evaluate_returns_three_for_prediction_mismatch(tmp_path: Path) -> None:
    labels_path = ROOT / "data" / "eval" / "expected_results.json"
    payload = json.loads(labels_path.read_text(encoding="utf-8"))
    payload["labels"][0]["expected_priority"] = "P5"
    labels_path = tmp_path / "expected.json"
    labels_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = runner.invoke(app, ["evaluate", "--labels", str(labels_path)])

    assert result.exit_code == 3
    assert "FAIL" in result.stdout
    assert "sample-001-course-registration" in result.stdout


def test_evaluate_reports_missing_labels(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["evaluate", "--labels", str(tmp_path / "missing.json")],
    )

    assert result.exit_code == 1
    assert "does not exist" in result.stderr


def test_validate_llm_requires_local_provider_config(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["validate-llm", "--llm-config", str(tmp_path / "missing.local.yaml")],
    )

    assert result.exit_code == 1
    assert "does not exist" in result.stderr


def test_validate_llm_limit_runs_one_message_without_missing_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_payload = json.loads(
        (ROOT / "data" / "eval" / "expected_results.json").read_text(encoding="utf-8")
    )
    label = expected_payload["labels"][0]
    provider = FakeLLMProvider(
        {
            label["source_id"]: LLMMessageAnalysis(
                priority=Priority(label["expected_priority"]),
                category=MessageCategory(label["expected_category"]),
                summary="Synthetic smoke-test summary.",
                action_items=(),
                deadline=None,
                confidence=0.9,
                rationale="Synthetic smoke-test rationale.",
                requires_review=label["requires_review"],
            )
        }
    )
    monkeypatch.setattr(
        OpenAICompatibleProvider,
        "from_yaml",
        classmethod(lambda cls, path: provider),
    )

    result = runner.invoke(
        app,
        [
            "validate-llm",
            "--llm-config",
            "unused.local.yaml",
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert "1/1" in result.stdout
    assert "未生成结构化分析" not in result.stdout


def test_no_command_displays_help() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "demo" in result.stdout
    assert "analyze" in result.stdout
    assert "evaluate" in result.stdout
    assert "validate-llm" in result.stdout
    assert "actions" in result.stdout


def test_actions_build_list_show_and_review_are_local(tmp_path: Path) -> None:
    queue_path = tmp_path / "data/private/action_queue.json"
    audit_path = tmp_path / "data/private/audit/actions.jsonl"
    build = runner.invoke(
        app,
        [
            "actions",
            "build",
            "--dataset",
            str(DATASET_PATH),
            "--queue",
            str(queue_path),
            "--audit-log",
            str(audit_path),
        ],
    )

    assert build.exit_code == 0
    assert "生成动作" in build.stdout
    assert "50" in build.stdout
    queue_payload = json.loads(queue_path.read_text(encoding="utf-8"))
    first_id = queue_payload["actions"][0]["action_id"]
    second_id = queue_payload["actions"][1]["action_id"]

    listed = runner.invoke(
        app,
        ["actions", "list", "--queue", str(queue_path), "--format", "json"],
    )
    assert listed.exit_code == 0
    assert len(json.loads(listed.stdout)["actions"]) == 50

    shown = runner.invoke(
        app,
        ["actions", "show", first_id, "--queue", str(queue_path), "--format", "json"],
    )
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["action_id"] == first_id

    approved = runner.invoke(
        app,
        [
            "actions",
            "approve",
            first_id,
            "--queue",
            str(queue_path),
            "--audit-log",
            str(audit_path),
        ],
    )
    rejected = runner.invoke(
        app,
        [
            "actions",
            "reject",
            second_id,
            "--queue",
            str(queue_path),
            "--audit-log",
            str(audit_path),
            "--reason",
            "测试拒绝",
        ],
    )
    assert approved.exit_code == 0
    assert "approved" in approved.stdout
    assert rejected.exit_code == 0
    assert "rejected" in rejected.stdout

    updated = json.loads(queue_path.read_text(encoding="utf-8"))
    statuses = {action["action_id"]: action["status"] for action in updated["actions"]}
    assert statuses[first_id] == "approved"
    assert statuses[second_id] == "rejected"
    audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 52
    assert all("message_id_sha256" in json.loads(line) for line in audit_lines)


def test_actions_show_missing_id_returns_clear_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["actions", "show", "action-missing", "--queue", str(tmp_path / "queue.json")],
    )

    assert result.exit_code == 1
    assert "does not exist" in result.stderr


def test_actions_apply_requires_dry_run_and_never_changes_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_path = tmp_path / "data/private/action_queue.json"
    audit_path = tmp_path / "data/private/audit/actions.jsonl"
    build = runner.invoke(
        app,
        [
            "actions",
            "build",
            "--dataset",
            str(DATASET_PATH),
            "--queue",
            str(queue_path),
            "--audit-log",
            str(audit_path),
        ],
    )
    assert build.exit_code == 0
    payload = json.loads(queue_path.read_text(encoding="utf-8"))
    action_id = payload["actions"][0]["action_id"]
    approved = runner.invoke(
        app,
        [
            "actions",
            "approve",
            action_id,
            "--queue",
            str(queue_path),
            "--audit-log",
            str(audit_path),
        ],
    )
    assert approved.exit_code == 0
    queue_before = queue_path.read_bytes()

    def forbidden_graph_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry-run must not construct GraphMailClient")

    monkeypatch.setattr("inbox_agent.cli.GraphMailClient", forbidden_graph_client)

    missing_flag = runner.invoke(
        app,
        [
            "actions",
            "apply",
            "--queue",
            str(queue_path),
            "--audit-log",
            str(audit_path),
        ],
    )
    dry_run = runner.invoke(
        app,
        [
            "actions",
            "apply",
            "--dry-run",
            "--queue",
            str(queue_path),
            "--audit-log",
            str(audit_path),
            "--format",
            "json",
        ],
    )

    assert missing_flag.exit_code == 1
    assert "only actions apply --dry-run" in missing_flag.stderr
    assert dry_run.exit_code == 0
    report = json.loads(dry_run.stdout)
    assert report["eligible_count"] == 1
    assert report["graph_write_request_count"] == 0
    assert queue_path.read_bytes() == queue_before
    audit_events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert sum(event["event_type"] == "dry_run_planned" for event in audit_events) == 1


def test_actions_rollback_requires_succeeded_action_and_never_writes_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_path = tmp_path / "data/private/action_queue.json"
    audit_path = tmp_path / "data/private/audit/actions.jsonl"
    build = runner.invoke(
        app,
        [
            "actions",
            "build",
            "--dataset",
            str(DATASET_PATH),
            "--queue",
            str(queue_path),
            "--audit-log",
            str(audit_path),
        ],
    )
    assert build.exit_code == 0
    repository = ActionQueueRepository(queue_path)
    first = repository.load().actions[0]
    approved = runner.invoke(
        app,
        [
            "actions",
            "approve",
            first.action_id,
            "--queue",
            str(queue_path),
            "--audit-log",
            str(audit_path),
        ],
    )
    assert approved.exit_code == 0
    approved_action = repository.load().find(first.action_id)
    assert approved_action is not None
    assert approved_action.idempotency_key is not None
    repository.claim_execution(first.action_id, approved_action.idempotency_key)
    repository.complete_execution(first.action_id, approved_action.idempotency_key)
    queue_before = queue_path.read_bytes()

    def forbidden_graph_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("rollback dry-run must not construct GraphMailClient")

    monkeypatch.setattr("inbox_agent.cli.GraphMailClient", forbidden_graph_client)
    missing_flag = runner.invoke(
        app,
        [
            "actions",
            "rollback",
            first.action_id,
            "--reason",
            "Classification was incorrect",
            "--queue",
            str(queue_path),
        ],
    )
    dry_run = runner.invoke(
        app,
        [
            "actions",
            "rollback",
            first.action_id,
            "--reason",
            "Classification was incorrect",
            "--dry-run",
            "--queue",
            str(queue_path),
            "--audit-log",
            str(audit_path),
            "--format",
            "json",
        ],
    )

    assert missing_flag.exit_code == 1
    assert "only actions rollback --dry-run" in missing_flag.stderr
    assert dry_run.exit_code == 0
    report = json.loads(dry_run.stdout)
    assert report["plan"]["source_status"] == MailboxActionStatus.SUCCEEDED
    assert report["graph_write_request_count"] == 0
    assert queue_path.read_bytes() == queue_before
    audit_events = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert sum(event["event_type"] == "rollback_dry_run_planned" for event in audit_events) == 1


@pytest.mark.parametrize("confirmation", [None, "another-action"])
def test_actions_execute_confirmation_gate_precedes_all_graph_and_queue_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    confirmation: str | None,
) -> None:
    queue_path = tmp_path / "action_queue.json"
    queue_path.write_text('{"unchanged": true}', encoding="utf-8")
    queue_before = queue_path.read_bytes()

    def forbidden_config_load(*args: object, **kwargs: object) -> None:
        raise AssertionError("confirmation gate must run before configuration loading")

    monkeypatch.setattr("inbox_agent.cli.load_graph_write_settings", forbidden_config_load)
    arguments = [
        "actions",
        "execute",
        "action-one",
        "--idempotency-key",
        "key-one",
        "--queue",
        str(queue_path),
    ]
    if confirmation is not None:
        arguments.extend(["--confirm-action", confirmation])

    result = runner.invoke(app, arguments)

    assert result.exit_code == 1
    assert "write confirmation denied" in result.stderr
    assert "No Graph request was sent" in result.stderr
    assert queue_path.read_bytes() == queue_before


@pytest.mark.parametrize("confirmation", [None, "another-action"])
def test_actions_rollback_confirmation_gate_precedes_all_external_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    confirmation: str | None,
) -> None:
    queue_path = tmp_path / "action_queue.json"
    queue_path.write_text('{"unchanged": true}', encoding="utf-8")
    queue_before = queue_path.read_bytes()

    def forbidden_config_load(*args: object, **kwargs: object) -> None:
        raise AssertionError("rollback confirmation must precede configuration loading")

    monkeypatch.setattr("inbox_agent.cli.load_graph_write_settings", forbidden_config_load)
    arguments = [
        "actions",
        "rollback-execute",
        "action-one",
        "--reason",
        "Incorrect classification",
        "--rollback-idempotency-key",
        "a" * 64,
        "--queue",
        str(queue_path),
    ]
    if confirmation is not None:
        arguments.extend(["--confirm-action", confirmation])

    result = runner.invoke(app, arguments)

    assert result.exit_code == 1
    assert "rollback confirmation denied" in result.stderr
    assert "No Graph request was sent" in result.stderr
    assert queue_path.read_bytes() == queue_before


def test_actions_execute_uses_silent_auth_and_exactly_one_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeProvider:
        @classmethod
        def from_settings(cls, settings: object, project_root: Path) -> FakeProvider:
            calls["provider_settings"] = settings
            calls["project_root"] = project_root
            return cls()

        def acquire_silent(self) -> GraphAccessToken:
            calls["silent_auth"] = True
            return GraphAccessToken("secret-token")

        def login(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("execute must never start interactive login")

    class FakeGraphClient:
        def __init__(self, settings: object, http_client: object) -> None:
            calls["graph_client_settings"] = settings

    class FakeExecutor:
        def __init__(self, repository: object, graph_client: object, audit_log: object) -> None:
            calls["executor_created"] = True

        def execute(
            self,
            action_id: str,
            idempotency_key: str,
            token: GraphAccessToken,
        ) -> ActionGraphExecutionReport:
            calls["execute_args"] = (action_id, idempotency_key, token.access_token)
            return ActionGraphExecutionReport(
                action_id=action_id,
                outcome=ActionGraphExecutionOutcome.SUCCEEDED,
                final_status=MailboxActionStatus.SUCCEEDED,
                attempt_number=1,
                graph_read_request_count=1,
                graph_write_request_count=1,
            )

    monkeypatch.setattr("inbox_agent.cli.GraphTokenProvider", FakeProvider)
    monkeypatch.setattr("inbox_agent.cli.GraphCategoryWriteClient", FakeGraphClient)
    monkeypatch.setattr("inbox_agent.cli.ApprovedActionGraphExecutor", FakeExecutor)
    action_id = "action-one"
    result = runner.invoke(
        app,
        [
            "actions",
            "execute",
            action_id,
            "--idempotency-key",
            "key-one",
            "--confirm-action",
            action_id,
            "--graph-config",
            str(write_graph_write_config(tmp_path, enabled=True)),
            "--queue",
            str(tmp_path / "queue.json"),
            "--audit-log",
            str(tmp_path / "audit.jsonl"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "succeeded"
    assert payload["graph_read_request_count"] == 1
    assert payload["graph_write_request_count"] == 1
    assert calls["silent_auth"] is True
    assert calls["execute_args"] == (action_id, "key-one", "secret-token")


def test_actions_execute_disabled_write_config_stops_before_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ForbiddenProvider:
        @classmethod
        def from_settings(cls, *args: object, **kwargs: object) -> None:
            raise AssertionError("disabled write configuration must stop before authentication")

    monkeypatch.setattr("inbox_agent.cli.GraphTokenProvider", ForbiddenProvider)
    action_id = "action-disabled"
    result = runner.invoke(
        app,
        [
            "actions",
            "execute",
            action_id,
            "--idempotency-key",
            "key-disabled",
            "--confirm-action",
            action_id,
            "--graph-config",
            str(write_graph_write_config(tmp_path, enabled=False)),
        ],
    )

    assert result.exit_code == 1
    assert "write_enabled: true" in result.stderr


def test_actions_execute_non_success_has_distinct_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProvider:
        @classmethod
        def from_settings(cls, *args: object, **kwargs: object) -> FakeProvider:
            return cls()

        def acquire_silent(self) -> GraphAccessToken:
            return GraphAccessToken("secret-token")

    class FakeGraphClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class FakeExecutor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def execute(
            self,
            action_id: str,
            idempotency_key: str,
            token: GraphAccessToken,
        ) -> ActionGraphExecutionReport:
            return ActionGraphExecutionReport(
                action_id=action_id,
                outcome=ActionGraphExecutionOutcome.CONFLICT,
                final_status=MailboxActionStatus.FAILED,
                attempt_number=1,
                graph_read_request_count=1,
                graph_write_request_count=0,
                reason="approved snapshot is stale",
            )

    monkeypatch.setattr("inbox_agent.cli.GraphTokenProvider", FakeProvider)
    monkeypatch.setattr("inbox_agent.cli.GraphCategoryWriteClient", FakeGraphClient)
    monkeypatch.setattr("inbox_agent.cli.ApprovedActionGraphExecutor", FakeExecutor)
    action_id = "action-conflict"
    result = runner.invoke(
        app,
        [
            "actions",
            "execute",
            action_id,
            "--idempotency-key",
            "key-conflict",
            "--confirm-action",
            action_id,
            "--graph-config",
            str(write_graph_write_config(tmp_path, enabled=True)),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["outcome"] == "conflict"


def test_actions_reconcile_is_single_action_read_only_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class FakeProvider:
        @classmethod
        def from_settings(cls, *args: object, **kwargs: object) -> FakeProvider:
            return cls()

        def acquire_silent(self) -> GraphAccessToken:
            calls["silent_auth"] = True
            return GraphAccessToken("secret-token")

    class FakeGraphClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class FakeReconciler:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def reconcile(
            self,
            action_id: str,
            idempotency_key: str,
            token: GraphAccessToken,
        ) -> ActionReconciliationReport:
            calls["reconcile_args"] = (action_id, idempotency_key, token.access_token)
            return ActionReconciliationReport(
                action_id=action_id,
                outcome=ActionReconciliationOutcome.APPLIED,
                final_status=MailboxActionStatus.SUCCEEDED,
                reason="live categories match the approved target",
            )

    monkeypatch.setattr("inbox_agent.cli.GraphTokenProvider", FakeProvider)
    monkeypatch.setattr("inbox_agent.cli.GraphCategoryWriteClient", FakeGraphClient)
    monkeypatch.setattr("inbox_agent.cli.UncertainActionReconciler", FakeReconciler)
    result = runner.invoke(
        app,
        [
            "actions",
            "reconcile",
            "action-uncertain",
            "--idempotency-key",
            "key-uncertain",
            "--graph-config",
            str(write_graph_write_config(tmp_path, enabled=True)),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "applied"
    assert payload["graph_read_request_count"] == 1
    assert payload["graph_write_request_count"] == 0
    assert calls["silent_auth"] is True
    assert calls["reconcile_args"] == (
        "action-uncertain",
        "key-uncertain",
        "secret-token",
    )


def test_actions_reconcile_unresolved_result_has_distinct_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProvider:
        @classmethod
        def from_settings(cls, *args: object, **kwargs: object) -> FakeProvider:
            return cls()

        def acquire_silent(self) -> GraphAccessToken:
            return GraphAccessToken("secret-token")

    class FakeGraphClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class FakeReconciler:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def reconcile(
            self,
            action_id: str,
            idempotency_key: str,
            token: GraphAccessToken,
        ) -> ActionReconciliationReport:
            return ActionReconciliationReport(
                action_id=action_id,
                outcome=ActionReconciliationOutcome.READ_FAILED,
                final_status=MailboxActionStatus.OUTCOME_UNKNOWN,
                reason="Graph read failed",
            )

    monkeypatch.setattr("inbox_agent.cli.GraphTokenProvider", FakeProvider)
    monkeypatch.setattr("inbox_agent.cli.GraphCategoryWriteClient", FakeGraphClient)
    monkeypatch.setattr("inbox_agent.cli.UncertainActionReconciler", FakeReconciler)
    result = runner.invoke(
        app,
        [
            "actions",
            "reconcile",
            "action-uncertain",
            "--idempotency-key",
            "key-uncertain",
            "--graph-config",
            str(write_graph_write_config(tmp_path, enabled=True)),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["outcome"] == "read_failed"
