"""Programmatic Alembic entry points used by the CLI and tests."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from inbox_agent.storage.database import sqlite_url


def _alembic_config(database_path: Path) -> Config:
    script_location = Path(__file__).with_name("alembic")
    config = Config()
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
    return config


def upgrade_database(database_path: Path, revision: str = "head") -> None:
    """Create the private directory and upgrade the database transactionally."""

    resolved = database_path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(resolved), revision)


def current_revision(engine: Engine) -> str | None:
    """Return the applied Alembic revision, or ``None`` for an empty database."""

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision(database_path: Path) -> str | None:
    """Return the single latest bundled migration revision without opening SQLite."""

    return ScriptDirectory.from_config(_alembic_config(database_path)).get_current_head()
