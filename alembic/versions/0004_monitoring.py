"""Monitoring heartbeats, process marks, samples, and check state.

Revision ID: 0004_monitoring
Revises: 0003_audit_trace
Create Date: 2026-10-07

Additive only. This revision creates new tables and indexes. It does not add,
drop, or rewrite columns on existing tables, and it does not update or delete
existing rows. Downgrade drops only the objects this revision added.

These tables are operational. They are not part of the world snapshot document
and they are not read by combat or the ledger.

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_monitoring"
down_revision: Union[str, Sequence[str], None] = "0003_audit_trace"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=80), primary_key=True),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("hostname", sa.String(length=80), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_tick_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tick_duration_ms", sa.Float(), nullable=False),
        sa.Column("events_processed", sa.Integer(), nullable=False),
        sa.Column("tick_status", sa.String(length=20), nullable=False),
        sa.Column("pool_checked_out", sa.Integer(), nullable=True),
        sa.Column("pool_size", sa.Integer(), nullable=True),
        sa.Column("pool_overflow", sa.Integer(), nullable=True),
        sa.Column("pool_capacity", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "worker_process_marks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("worker_id", sa.String(length=80), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("wall_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_worker_process_marks_wall_at", "worker_process_marks", ["wall_at"])
    op.create_table(
        "monitoring_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
    )
    op.create_index("ix_monitoring_samples_sampled_at", "monitoring_samples", ["sampled_at"])
    op.create_index(
        "ix_monitoring_samples_metric_sampled_at",
        "monitoring_samples",
        ["metric", "sampled_at"],
    )
    op.create_table(
        "monitoring_check_state",
        sa.Column("check_name", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("monitoring_check_state")
    op.drop_index("ix_monitoring_samples_metric_sampled_at", table_name="monitoring_samples")
    op.drop_index("ix_monitoring_samples_sampled_at", table_name="monitoring_samples")
    op.drop_table("monitoring_samples")
    op.drop_index("ix_worker_process_marks_wall_at", table_name="worker_process_marks")
    op.drop_table("worker_process_marks")
    op.drop_table("worker_heartbeats")
