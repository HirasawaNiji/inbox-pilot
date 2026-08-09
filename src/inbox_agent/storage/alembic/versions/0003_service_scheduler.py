"""Persist local scheduler state.

Revision ID: 0003_service
Revises: 0002_workflow
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_service"
down_revision: str | None = "0002_workflow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the singleton-per-name scheduler status table."""

    op.create_table(
        "service_states",
        sa.Column("service_name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.String(length=40), nullable=True),
        sa.Column("last_run_at", sa.String(length=40), nullable=True),
        sa.Column("last_success_at", sa.String(length=40), nullable=True),
        sa.Column("last_failure_at", sa.String(length=40), nullable=True),
        sa.Column("next_run_at", sa.String(length=40), nullable=True),
        sa.Column("last_run_id", sa.String(length=64), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("service_name"),
    )


def downgrade() -> None:
    """Remove persisted scheduler status."""

    op.drop_table("service_states")
