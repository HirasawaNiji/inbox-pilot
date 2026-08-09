"""Create the initial Stage 4 persistence schema.

Revision ID: 0001_stage4
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_stage4"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create durable message, analysis, action, cursor, and run tables."""

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=512), nullable=False),
        sa.Column("internet_message_id", sa.String(length=998), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("received_at", sa.String(length=40), nullable=False),
        sa.Column("change_key", sa.String(length=512), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("normalized_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "source_id", name="uq_messages_source_identity"),
    )
    op.create_index("ix_messages_received_at", "messages", ["received_at"])

    op.create_table(
        "analyses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.String(length=2), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("decision_source", sa.String(length=16), nullable=False),
        sa.Column("requires_review", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(length=100), nullable=False),
        sa.Column("evaluated_at", sa.String(length=40), nullable=False),
        sa.Column("triage_json", sa.Text(), nullable=False),
        sa.Column("rule_json", sa.Text(), nullable=True),
        sa.Column("llm_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "fingerprint", name="uq_analyses_message_fingerprint"),
    )
    op.create_index("ix_analyses_priority_evaluated", "analyses", ["priority", "evaluated_at"])
    op.create_index(
        "ix_analyses_review_evaluated",
        "analyses",
        ["requires_review", "evaluated_at"],
    )

    op.create_table(
        "mailbox_actions",
        sa.Column("action_id", sa.String(length=128), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("action_id"),
    )
    op.create_index(
        "ix_mailbox_actions_status_updated", "mailbox_actions", ["status", "updated_at"]
    )

    op.create_table(
        "sync_cursors",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("mailbox_key", sa.String(length=320), nullable=False),
        sa.Column("folder_key", sa.String(length=256), nullable=False),
        sa.Column("cursor_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "mailbox_key", "folder_key", name="uq_sync_cursor_scope"),
    )

    op.create_table(
        "workflow_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.String(length=40), nullable=False),
        sa.Column("finished_at", sa.String(length=40), nullable=True),
        sa.Column("counters_json", sa.Text(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_workflow_runs_status_started", "workflow_runs", ["status", "started_at"])


def downgrade() -> None:
    """Remove the complete initial schema."""

    op.drop_index("ix_workflow_runs_status_started", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_table("sync_cursors")
    op.drop_index("ix_mailbox_actions_status_updated", table_name="mailbox_actions")
    op.drop_table("mailbox_actions")
    op.drop_index("ix_analyses_review_evaluated", table_name="analyses")
    op.drop_index("ix_analyses_priority_evaluated", table_name="analyses")
    op.drop_table("analyses")
    op.drop_index("ix_messages_received_at", table_name="messages")
    op.drop_table("messages")
