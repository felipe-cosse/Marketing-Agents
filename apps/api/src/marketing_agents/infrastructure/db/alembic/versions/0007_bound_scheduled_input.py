"""Persist explicit scheduled input and its configuration binding.

Revision ID: 0007
Revises: 0006

NULL preserves legacy integrity material without inventing operator input.
Runtime execution requires a bound, validated snapshot; old records remain readable.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_instance_configs", sa.Column("scheduled_input_json", sa.Text(), nullable=True)
    )
    op.add_column("schedules", sa.Column("configuration_revision", sa.Integer(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are unsupported; restore a verified backup instead.")
