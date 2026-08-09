"""Integration tests for the server-rendered local console."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from inbox_agent.actions import (
    ActionActor,
    ActionQueueRepository,
    MailboxActionStatus,
    build_review_actions,
)
from inbox_agent.loader import load_dataset
from inbox_agent.normalizer import normalize_message
from inbox_agent.pipeline import OfflinePipeline
from inbox_agent.storage import AnalysisRepository, Database, MessageRepository, upgrade_database
from inbox_agent.web import WebSettings, create_app

ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = ROOT / "data" / "samples" / "sample_emails.json"
POLICY_PATH = ROOT / "config" / "rules.yaml"


def settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        project_root=tmp_path,
        database_path=tmp_path / "private" / "inbox_pilot.sqlite3",
        action_queue_path=tmp_path / "private" / "actions.json",
        audit_log_path=tmp_path / "private" / "audit.jsonl",
        graph_write_config_path=tmp_path / "private" / "graph_write.local.yaml",
        service_config_path=tmp_path / "private" / "service.local.yaml",
    )


def seed(tmp_path: Path) -> tuple[WebSettings, str]:
    web_settings = settings(tmp_path)
    upgrade_database(web_settings.database_path)
    database = Database(web_settings.database_path)
    dataset = load_dataset(DATASET_PATH).model_copy(
        update={"messages": load_dataset(DATASET_PATH).messages[:3]}
    )
    report = OfflinePipeline.from_yaml(POLICY_PATH).analyze_dataset(dataset)
    results = {result.message_id: result for result in report.results}
    rules = {
        result.message_id: rule
        for result, rule in zip(report.results, report.rule_evaluations, strict=True)
    }
    messages = MessageRepository(database)
    analyses = AnalysisRepository(database)
    try:
        for message in dataset.messages:
            messages.upsert(message)
            messages.save_normalized(normalize_message(message))
            analyses.save(
                source=message.source,
                result=results[message.source_id],
                rule_evaluation=rules[message.source_id],
            )
    finally:
        database.dispose()
    actions = build_review_actions(dataset, report)
    ActionQueueRepository(web_settings.action_queue_path).enqueue(actions)
    return web_settings, actions[0].action_id


def hidden_value(response_text: str, name: str) -> str:
    element = BeautifulSoup(response_text, "html.parser").find("input", attrs={"name": name})
    assert element is not None
    value = element.get("value")
    assert isinstance(value, str)
    return value


def test_console_missing_database_renders_safe_setup_error(tmp_path: Path) -> None:
    web_settings = settings(tmp_path)

    with TestClient(create_app(web_settings)) as client:
        response = client.get("/console")

    assert response.status_code == 503
    assert "DATABASE_UNAVAILABLE" in response.text
    assert "The local database is unavailable" in response.text
    assert not web_settings.database_path.exists()


def test_dashboard_inbox_and_explanation_pages_render_real_sqlite_data(
    tmp_path: Path,
) -> None:
    web_settings, _ = seed(tmp_path)

    with TestClient(create_app(web_settings)) as client:
        dashboard = client.get("/console")
        inbox = client.get("/console/inbox")
        partial = client.get(
            "/console/inbox/table",
            params={"priority": "P4"},
            headers={"HX-Request": "true"},
        )
        first_id = client.get("/api/v1/messages", params={"limit": 1}).json()["items"][0][
            "database_id"
        ]
        detail = client.get(f"/console/messages/{first_id}")
        css = client.get("/static/console.css")
        settings_js = client.get("/static/settings.js")

    assert dashboard.status_code == 200
    assert "今天的收件箱" in dashboard.text
    assert "Content-Security-Policy" in dashboard.headers
    assert "inboxpilot_csrf" in dashboard.cookies
    assert inbox.status_code == 200
    assert "收件箱分析" in inbox.text
    assert partial.status_code == 200
    assert "message-results" in partial.text
    assert "<!doctype html>" not in partial.text.lower()
    assert detail.status_code == 200
    assert "最终融合结果" in detail.text
    assert "YAML 规则结果" in detail.text
    assert "LLM 结果" in detail.text
    assert "冲突与复核" in detail.text
    assert css.status_code == 200
    assert "--accent: #0067c0" in css.text
    assert "linear-gradient(180deg, #1689e8 0%, #0067c0 64%, #005a9e 100%)" in css.text
    assert ".priority-cell-p1" in css.text
    assert "--priority-color: #b42318" in css.text
    assert "-webkit-text-stroke: 2px white" in css.text
    assert settings_js.status_code == 200
    assert "updateModels" in settings_js.text


def test_console_review_and_operations_pages_are_available(tmp_path: Path) -> None:
    web_settings, _ = seed(tmp_path)

    with TestClient(create_app(web_settings)) as client:
        reviews = client.get("/console/reviews")
        actions = client.get("/console/actions")
        operations = client.get("/console/operations")

    assert reviews.status_code == 200
    assert "人工复核队列" in reviews.text
    assert "待确认写回" in reviews.text
    assert actions.status_code == 200
    assert "Outlook 动作队列" in actions.text
    assert operations.status_code == 200
    assert "工作流与运行状态" in operations.text


def test_console_approval_requires_csrf_and_reuses_action_queue(tmp_path: Path) -> None:
    web_settings, action_id = seed(tmp_path)

    with TestClient(create_app(web_settings)) as client:
        rejected = client.post(
            f"/console/actions/{action_id}/approve",
            data={"_csrf": "invalid-csrf-token", "note": "must not be accepted"},
        )
        detail = client.get(f"/console/actions/{action_id}")
        csrf = hidden_value(detail.text, "_csrf")
        approved = client.post(
            f"/console/actions/{action_id}/approve",
            data={"_csrf": csrf, "note": "Reviewed in the console"},
            follow_redirects=False,
        )

    action = ActionQueueRepository(web_settings.action_queue_path).load().find(action_id)
    assert rejected.status_code == 403
    assert "CSRF_REJECTED" in rejected.text
    assert detail.status_code == 200
    assert approved.status_code == 303
    assert action is not None
    assert action.status.value == "approved"
    assert web_settings.audit_log_path.is_file()


def test_htmx_approval_returns_updated_action_fragment(tmp_path: Path) -> None:
    web_settings, action_id = seed(tmp_path)

    with TestClient(create_app(web_settings)) as client:
        detail = client.get(f"/console/actions/{action_id}")
        csrf = hidden_value(detail.text, "_csrf")
        response = client.post(
            f"/console/actions/{action_id}/approve",
            data={"_csrf": csrf, "note": "Reviewed with HTMX"},
            headers={"HX-Request": "true"},
        )

    assert response.status_code == 200
    assert 'id="action-workspace"' in response.text
    assert "动作已批准" in response.text
    assert "预览实际变更" in response.text
    assert "<!doctype html>" not in response.text.lower()


def test_execute_page_repeats_exact_diff_and_confirmation_precedes_graph_config(
    tmp_path: Path,
) -> None:
    web_settings, action_id = seed(tmp_path)

    with TestClient(create_app(web_settings)) as client:
        detail = client.get(f"/console/actions/{action_id}")
        csrf = hidden_value(detail.text, "_csrf")
        client.post(
            f"/console/actions/{action_id}/approve",
            data={"_csrf": csrf, "note": "Ready for preview"},
        )
        confirmation = client.get(f"/console/actions/{action_id}/execute")
        execute_csrf = hidden_value(confirmation.text, "_csrf")
        idempotency_key = hidden_value(confirmation.text, "idempotency_key")
        mismatch = client.post(
            f"/console/actions/{action_id}/execute",
            data={
                "_csrf": execute_csrf,
                "confirm_action_id": "action-different-confirmation",
                "idempotency_key": idempotency_key,
            },
        )

    assert confirmation.status_code == 200
    assert "实际变更" in confirmation.text
    request_counter = BeautifulSoup(confirmation.text, "html.parser").select_one(".request-counter")
    assert request_counter is not None
    assert request_counter.get_text(" ", strip=True) == "0 次预览写请求"
    assert action_id in confirmation.text
    assert "所有非 `InboxPilot/` 用户类别都会被保留" in confirmation.text
    assert mismatch.status_code == 409
    assert "CONFIRMATION_MISMATCH" in mismatch.text
    assert "graph_write.local.yaml" not in mismatch.text


def test_succeeded_action_can_preview_a_controlled_rollback(tmp_path: Path) -> None:
    web_settings, action_id = seed(tmp_path)
    repository = ActionQueueRepository(web_settings.action_queue_path)
    repository.transition(action_id, MailboxActionStatus.APPROVED, actor=ActionActor.USER)
    repository.transition(action_id, MailboxActionStatus.EXECUTING, actor=ActionActor.SYSTEM)
    repository.transition(action_id, MailboxActionStatus.SUCCEEDED, actor=ActionActor.SYSTEM)

    with TestClient(create_app(web_settings)) as client:
        detail = client.get(f"/console/actions/{action_id}")
        csrf = hidden_value(detail.text, "_csrf")
        preview = client.post(
            f"/console/actions/{action_id}/rollback/preview",
            data={"_csrf": csrf, "reason": "The classification needs correction"},
        )
        rollback_csrf = hidden_value(preview.text, "_csrf")
        rollback_key = hidden_value(preview.text, "rollback_idempotency_key")
        mismatch = client.post(
            f"/console/actions/{action_id}/rollback/execute",
            data={
                "_csrf": rollback_csrf,
                "reason": "The classification needs correction",
                "confirm_action_id": "action-different-confirmation",
                "rollback_idempotency_key": rollback_key,
            },
        )

    assert detail.status_code == 200
    assert "生成回滚预览" in detail.text
    assert preview.status_code == 200
    assert "确认受控回滚" in preview.text
    assert "0" in preview.text
    assert "仅撤销 InboxPilot 管理的类别" in preview.text
    assert action_id in preview.text
    assert mismatch.status_code == 409
    assert "CONFIRMATION_MISMATCH" in mismatch.text
    assert "graph_write.local.yaml" not in mismatch.text
