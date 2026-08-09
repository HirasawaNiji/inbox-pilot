"""Persist notification delivery deduplication.

Revision ID: 0004_notifications
Revises: 0003_service
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_notifications"
down_revision: str | None = "0003_service"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the privacy-safe notification delivery ledger."""

    op.create_table(
        "notification_deliveries",
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("related_hash", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("last_attempt_at", sa.String(length=40), nullable=False),
        sa.Column("delivered_at", sa.String(length=40), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_notification_deliveries_status_kind",
        "notification_deliveries",
        ["status", "kind"],
    )


def downgrade() -> None:
    """Remove the notification delivery ledger."""

    op.drop_index(
        "ix_notification_deliveries_status_kind",
        table_name="notification_deliveries",
    )
    op.drop_table("notification_deliveries")
