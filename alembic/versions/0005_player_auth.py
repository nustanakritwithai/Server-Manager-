"""Player accounts, refresh sessions, and command idempotency keys.

Revision ID: 0005_player_auth
Revises: 0004_monitoring
Create Date: 2026-10-07

Additive only. This revision creates new tables and indexes. It does not add,
drop, or rewrite columns on existing tables, and it does not update or delete
existing rows. Downgrade drops only the objects this revision added.

These tables are operational. They are not part of the world snapshot document.
``player_accounts.player_id`` is nullable and is not a foreign key, so snapshot
restore can replace ``players`` without deleting credentials. Refresh sessions
reference accounts only. Command idempotency keys are also outside the snapshot;
restore deletes them in application code so a replay cannot return a result from
a world that was rolled back.

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_player_auth"
down_revision: Union[str, Sequence[str], None] = "0004_monitoring"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("username_key", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("email_key", sa.String(length=254), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("login_locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_player_accounts_username_key", "player_accounts", ["username_key"], unique=True)
    op.create_index("uq_player_accounts_player_id", "player_accounts", ["player_id"], unique=True)
    op.create_index(
        "uq_player_accounts_email_key",
        "player_accounts",
        ["email_key"],
        unique=True,
        postgresql_where=sa.text("email_key IS NOT NULL"),
    )
    op.create_table(
        "player_refresh_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("player_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=200), nullable=True),
    )
    op.create_index("ix_player_refresh_sessions_account_id", "player_refresh_sessions", ["account_id"])
    op.create_index("ix_player_refresh_sessions_family_id", "player_refresh_sessions", ["family_id"])
    op.create_index("uq_player_refresh_sessions_token_hash", "player_refresh_sessions", ["token_hash"], unique=True)
    op.create_table(
        "command_idempotency_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("player_id", "idempotency_key", name="uq_command_idempotency_player_key"),
    )


def downgrade() -> None:
    op.drop_table("command_idempotency_keys")
    op.drop_index("uq_player_refresh_sessions_token_hash", table_name="player_refresh_sessions")
    op.drop_index("ix_player_refresh_sessions_family_id", table_name="player_refresh_sessions")
    op.drop_index("ix_player_refresh_sessions_account_id", table_name="player_refresh_sessions")
    op.drop_table("player_refresh_sessions")
    op.drop_index("uq_player_accounts_email_key", table_name="player_accounts")
    op.drop_index("uq_player_accounts_player_id", table_name="player_accounts")
    op.drop_index("uq_player_accounts_username_key", table_name="player_accounts")
    op.drop_table("player_accounts")
