"""Add privacy-bounded observability events.

Revision ID: 0005_observability
Revises: 0004_notifications
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_observability"
down_revision: str | None = "0004_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the privacy-bounded event ledger."""

    op.create_table(
        "observability_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("message_hash", sa.String(length=64), nullable=True),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_microusd", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=100), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_observability_events_run_time", "observability_events", ["run_id", "occurred_at"]
    )
    op.create_index(
        "ix_observability_events_message_time",
        "observability_events",
        ["message_hash", "occurred_at"],
    )
    op.create_index(
        "ix_observability_events_provider_outcome", "observability_events", ["provider", "outcome"]
    )


def downgrade() -> None:
    """Remove observability events."""

    op.drop_index("ix_observability_events_provider_outcome", table_name="observability_events")
    op.drop_index("ix_observability_events_message_time", table_name="observability_events")
    op.drop_index("ix_observability_events_run_time", table_name="observability_events")
    op.drop_table("observability_events")
