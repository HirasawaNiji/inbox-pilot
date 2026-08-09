"""Add durable incremental workflow state.

Revision ID: 0002_workflow
Revises: 0001_stage4
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_workflow"
down_revision: str | None = "0001_stage4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_PROFILE = "0" * 64


def upgrade() -> None:
    """Add configuration-aware analysis idempotency and step progress."""

    with op.batch_alter_table("analyses") as batch_op:
        batch_op.add_column(sa.Column("message_content_hash", sa.String(length=64)))
        batch_op.add_column(sa.Column("analysis_profile", sa.String(length=64)))
        batch_op.add_column(sa.Column("complete", sa.Integer()))

    op.execute(
        sa.text(
            "UPDATE analyses SET message_content_hash = "
            "(SELECT messages.content_hash FROM messages WHERE messages.id = analyses.message_id)"
        )
    )
    op.execute(
        sa.text("UPDATE analyses SET analysis_profile = :profile, complete = 1").bindparams(
            profile=_EMPTY_PROFILE
        )
    )
    with op.batch_alter_table("analyses") as batch_op:
        batch_op.alter_column("message_content_hash", existing_type=sa.String(64), nullable=False)
        batch_op.alter_column("analysis_profile", existing_type=sa.String(64), nullable=False)
        batch_op.alter_column("complete", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_constraint("uq_analyses_message_fingerprint", type_="unique")
        batch_op.create_unique_constraint(
            "uq_analyses_current_profile",
            ["message_id", "message_content_hash", "analysis_profile"],
        )

    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.add_column(sa.Column("current_step", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("steps_json", sa.Text(), nullable=True))
    op.execute(sa.text("UPDATE workflow_runs SET steps_json = '[]' WHERE steps_json IS NULL"))
    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.alter_column("steps_json", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    """Return to the initial Stage 4 persistence schema."""

    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.drop_column("steps_json")
        batch_op.drop_column("current_step")

    with op.batch_alter_table("analyses") as batch_op:
        batch_op.drop_constraint("uq_analyses_current_profile", type_="unique")
        batch_op.create_unique_constraint(
            "uq_analyses_message_fingerprint",
            ["message_id", "fingerprint"],
        )
        batch_op.drop_column("complete")
        batch_op.drop_column("analysis_profile")
        batch_op.drop_column("message_content_hash")
