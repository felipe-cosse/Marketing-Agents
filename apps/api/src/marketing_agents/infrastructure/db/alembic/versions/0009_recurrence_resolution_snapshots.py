"""Persist observed recurrence resolutions without inventing historical evidence.

Revision ID: 0009
Revises: 0008

NULL means the historical calculation has no recorded snapshot. Existing rows,
identities and integrity digests are untouched; only new observations use JSON.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schedules", sa.Column("next_recurrence_json", sa.Text(), nullable=True))
    op.add_column(
        "schedule_occurrences", sa.Column("scheduled_recurrence_json", sa.Text(), nullable=True)
    )
    op.add_column(
        "schedule_occurrences", sa.Column("next_recurrence_json", sa.Text(), nullable=True)
    )
    op.add_column(
        "schedule_occurrences", sa.Column("recurrence_resolutions_json", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are unsupported; restore a verified backup instead.")
