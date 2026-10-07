"""Nullable trace ids, player_commands, and the append-only audit log.

Revision ID: 0003_audit_trace
Revises: 0002_world_snapshots
Create Date: 2026-10-07

Additive only. Existing rows are not rewritten: new columns are nullable and
have no server default, so values already stored stay as they are and the new
columns are NULL. New tables start empty. Downgrade drops only the objects
this revision added.

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_audit_trace"
down_revision: Union[str, Sequence[str], None] = "0002_world_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("movements", sa.Column("trace_id", sa.String(length=36), nullable=True))
    op.add_column("events", sa.Column("trace_id", sa.String(length=36), nullable=True))
    op.add_column("battle_reports", sa.Column("trace_id", sa.String(length=36), nullable=True))
    op.add_column("transactions", sa.Column("trace_id", sa.String(length=36), nullable=True))
    op.create_index("ix_movements_trace_id", "movements", ["trace_id"])
    op.create_index("ix_events_trace_id", "events", ["trace_id"])
    op.create_index("ix_battle_reports_trace_id", "battle_reports", ["trace_id"])
    op.create_index("ix_transactions_trace_id", "transactions", ["trace_id"])

    op.create_table(
        "player_commands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trace_id", sa.String(length=36), nullable=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("command_type", sa.String(length=20), nullable=False),
        sa.Column("army_id", sa.Integer(), sa.ForeignKey("armies.id"), nullable=True),
        sa.Column("target", postgresql.JSONB(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_player_commands_player_id", "player_commands", ["player_id"])
    op.create_index("ix_player_commands_army_id", "player_commands", ["army_id"])
    op.create_index("uq_player_commands_trace_id", "player_commands", ["trace_id"], unique=True)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=200), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_ip", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("row_hash", sa.String(length=64), nullable=False),
    )
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_occurred_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("uq_player_commands_trace_id", table_name="player_commands")
    op.drop_index("ix_player_commands_army_id", table_name="player_commands")
    op.drop_index("ix_player_commands_player_id", table_name="player_commands")
    op.drop_table("player_commands")
    op.drop_index("ix_transactions_trace_id", table_name="transactions")
    op.drop_index("ix_battle_reports_trace_id", table_name="battle_reports")
    op.drop_index("ix_events_trace_id", table_name="events")
    op.drop_index("ix_movements_trace_id", table_name="movements")
    op.drop_column("transactions", "trace_id")
    op.drop_column("battle_reports", "trace_id")
    op.drop_column("events", "trace_id")
    op.drop_column("movements", "trace_id")
