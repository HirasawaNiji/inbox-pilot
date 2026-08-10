"""Stage 4 step 8 deployment asset safety tests."""

from __future__ import annotations

from pathlib import Path

import yaml

from inbox_agent.service import load_service_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_required_deployment_assets_exist() -> None:
    required = (
        ".env.example",
        ".dockerignore",
        "Dockerfile",
        "compose.yaml",
        "config/service.personal.example.yaml",
        "scripts/Install-InboxPilot.ps1",
        "scripts/Start-InboxPilot.ps1",
        "scripts/Test-Deployment.ps1",
    )

    assert all((PROJECT_ROOT / path).is_file() for path in required)


def test_personal_service_template_is_safe_by_default() -> None:
    settings = load_service_settings(PROJECT_ROOT / "config/service.personal.example.yaml")

    assert settings.workflow.sync_outlook is False
    assert settings.workflow.llm_config_path is None
    assert settings.workflow.database_path == Path("data/private/inbox_pilot.sqlite3")
    assert settings.observability.enabled is True
    assert settings.observability.llm_pricing == ()


def test_compose_is_loopback_only_and_has_no_automatic_restart() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["inbox-pilot"]

    assert service["ports"] == ["127.0.0.1:8765:8765"]
    assert "restart" not in service
    assert service["env_file"] == [".env"]
    assert "./data/private:/app/data/private" in service["volumes"]
    assert "./config:/app/config:ro" in service["volumes"]


def test_examples_contain_no_credentials() -> None:
    env_lines = [
        line.strip()
        for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert env_lines == ["OPENAI_API_KEY=", "DEEPSEEK_API_KEY="]


def test_windows_scripts_preserve_private_files_and_use_hidden_background_process() -> None:
    installer = (PROJECT_ROOT / "scripts/Install-InboxPilot.ps1").read_text(encoding="utf-8")
    launcher = (PROJECT_ROOT / "scripts/Start-InboxPilot.ps1").read_text(encoding="utf-8")

    assert '"sync", "--locked"' in installer
    assert "Preserved existing private file" in installer
    assert "-WindowStyle Hidden" in launcher
    assert 'if ($Background -and $Mode -ne "Web")' in launcher
    assert "Remove-Item" not in installer + launcher


def test_container_runs_as_non_root_and_excludes_private_state() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "USER inboxpilot" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile
    assert ".env" in dockerignore
    assert "config/*.local.yaml" in dockerignore
    assert "data/private" in dockerignore
