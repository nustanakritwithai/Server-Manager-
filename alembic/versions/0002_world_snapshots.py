"""World snapshots and the world_version / maintenance gates.

Revision ID: 0002_world_snapshots
Revises: 0001_initial
Create Date: 2026-10-07

Additive only. Existing world_state rows gain columns with defaults.
Snapshot tables are new. This is not a backup format.

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_world_snapshots"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "world_state",
        sa.Column("world_version", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "world_state",
        sa.Column("commands_open", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "world_state",
        sa.Column("worker_paused", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "world_state",
        sa.Column("restore_active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "world_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("world_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("world_version", sa.BigInteger(), nullable=False),
        sa.Column("checksum", sa.String(length=80), nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "reason IN ('AUTO', 'MANUAL', 'SAFETY')",
            name="ck_snapshot_reason",
        ),
        sa.CheckConstraint(
            "status IN ('CREATING', 'READY', 'FAILED', 'RESTORING')",
            name="ck_snapshot_status",
        ),
    )
    op.create_index("ix_world_snapshots_created_at", "world_snapshots", ["created_at"])

    op.create_table(
        "world_snapshot_payloads",
        sa.Column("snapshot_id", sa.Integer(), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["world_snapshots.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("world_snapshot_payloads")
    op.drop_index("ix_world_snapshots_created_at", table_name="world_snapshots")
    op.drop_table("world_snapshots")
    op.drop_column("world_state", "restore_active")
    op.drop_column("world_state", "worker_paused")
    op.drop_column("world_state", "commands_open")
    op.drop_column("world_state", "world_version")
