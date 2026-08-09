"""Integration tests for the loopback-only FastAPI surface."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from inbox_agent.actions import ActionQueueRepository, MailboxActionStatus, build_review_actions
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
            result = results[message.source_id]
            rule = rules[message.source_id]
            messages.upsert(message)
            messages.save_normalized(normalize_message(message))
            analyses.save(source=message.source, result=result, rule_evaluation=rule)
    finally:
        database.dispose()
    actions = build_review_actions(dataset, report)
    ActionQueueRepository(web_settings.action_queue_path).enqueue(actions)
    return web_settings, actions[0].action_id


def test_docs_and_health_start_without_creating_a_missing_database(tmp_path: Path) -> None:
    web_settings = settings(tmp_path)

    with TestClient(create_app(web_settings)) as client:
        docs = client.get("/docs")
        health = client.get("/api/v1/health")

    assert docs.status_code == 200
    assert health.status_code == 200
    assert health.json()["database_ready"] is False
    assert not web_settings.database_path.exists()


def test_openapi_exposes_the_stage_four_route_contract(tmp_path: Path) -> None:
    web_settings = settings(tmp_path)
    expected_paths = {
        "/api/v1/health",
        "/api/v1/messages",
        "/api/v1/messages/{database_id}",
        "/api/v1/reviews",
        "/api/v1/actions",
        "/api/v1/actions/{action_id}",
        "/api/v1/actions/{action_id}/approve",
        "/api/v1/actions/{action_id}/reject",
        "/api/v1/actions/{action_id}/preview",
        "/api/v1/actions/{action_id}/execute",
        "/api/v1/actions/{action_id}/reconcile",
        "/api/v1/actions/{action_id}/rollback/preview",
        "/api/v1/actions/{action_id}/rollback/execute",
        "/api/v1/actions/{action_id}/rollback/reconcile",
        "/api/v1/workflows/runs/latest",
        "/api/v1/workflows/runs/{run_id}",
        "/api/v1/service/status",
    }

    with TestClient(create_app(web_settings)) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    assert expected_paths <= set(response.json()["paths"])


def test_message_queries_read_sqlite_and_include_explanations(tmp_path: Path) -> None:
    web_settings, _ = seed(tmp_path)

    with TestClient(create_app(web_settings)) as client:
        page = client.get("/api/v1/messages", params={"limit": 2})
        first = page.json()["items"][0]
        filtered = client.get(
            "/api/v1/messages",
            params={"priority": first["priority"], "category": first["category"]},
        )
        detail = client.get(f"/api/v1/messages/{first['database_id']}")

    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 2
    assert filtered.status_code == 200
    assert filtered.json()["total"] >= 1
    assert detail.status_code == 200
    assert detail.json()["triage"]["message_id"] == first["source_id"]
    assert detail.json()["rule_evaluation"] is not None


def test_review_endpoints_reuse_locked_queue_and_append_audit(tmp_path: Path) -> None:
    web_settings, action_id = seed(tmp_path)

    with TestClient(create_app(web_settings)) as client:
        pending = client.get(
            "/api/v1/actions",
            params={"status": MailboxActionStatus.PENDING_REVIEW.value},
        )
        approved = client.post(
            f"/api/v1/actions/{action_id}/approve",
            json={"note": "Reviewed in the local API"},
        )
        preview = client.post(f"/api/v1/actions/{action_id}/preview")

    assert pending.status_code == 200
    assert pending.json()["total"] == 3
    assert approved.status_code == 200
    assert approved.json()["status"] == MailboxActionStatus.APPROVED.value
    assert preview.status_code == 200
    assert preview.json()["eligible_count"] == 1
    assert preview.json()["graph_write_request_count"] == 0
    assert web_settings.audit_log_path.is_file()


def test_write_confirmation_gate_runs_before_graph_configuration(tmp_path: Path) -> None:
    web_settings, action_id = seed(tmp_path)

    with TestClient(create_app(web_settings)) as client:
        response = client.post(
            f"/api/v1/actions/{action_id}/execute",
            json={
                "confirm_action_id": "action-different-confirmation",
                "idempotency_key": "a" * 64,
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFIRMATION_MISMATCH"
    assert "graph_write.local.yaml" not in response.text


def test_validation_errors_do_not_echo_sensitive_request_values(tmp_path: Path) -> None:
    web_settings, action_id = seed(tmp_path)
    secret = "super-secret-api-key-value"

    with TestClient(create_app(web_settings)) as client:
        response = client.post(
            f"/api/v1/actions/{action_id}/execute",
            json={"confirm_action_id": secret, "idempotency_key": secret},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert secret not in response.text
