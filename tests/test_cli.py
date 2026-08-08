"""Command-line integration tests for InboxPilot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from inbox_agent.cli import app
from inbox_agent.graph import GraphAccessToken, GraphSyncReport

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


def test_demo_runs_bundled_dataset() -> None:
    result = runner.invoke(app, ["demo"])

    assert result.exit_code == 0
    assert "InboxPilot Analysis" in result.stdout
    assert "成功 20" in result.stdout
    assert "待复核 3" in result.stdout
    assert "sample-" not in result.stdout


def test_demo_json_is_machine_readable() -> None:
    result = runner.invoke(app, ["demo", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["policy_version"] == "rules-v1"
    assert len(payload["results"]) == 20
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
    assert len(payload["results"]) == 20


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


def test_no_command_displays_help() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Usage:" in result.stdout
    assert "demo" in result.stdout
    assert "analyze" in result.stdout
    assert "evaluate" in result.stdout
