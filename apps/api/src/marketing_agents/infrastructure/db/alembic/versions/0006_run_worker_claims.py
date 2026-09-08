"""Persist fenced Run worker leases.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_worker_claims",
        sa.Column("run_id", sa.String(240), nullable=False),
        sa.Column("owner", sa.String(240), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_run_worker_claim_version"),
        sa.CheckConstraint("expires_at > claimed_at", name="ck_run_worker_claim_times"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_worker_claims_run_id_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_worker_claims")),
    )


def downgrade() -> None:
    raise RuntimeError("Destructive downgrades are unsupported; restore a verified backup instead.")
