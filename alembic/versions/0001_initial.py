"""Initial simulation schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-06

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "world_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("offset_seconds", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("INSERT INTO world_state (id, offset_seconds) VALUES (1, 0)")

    op.create_table(
        "players",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("research", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_players_name"),
    )

    op.create_table(
        "cities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("x", sa.Integer(), nullable=False),
        sa.Column("y", sa.Integer(), nullable=False),
        sa.Column("wood", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("food", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("iron", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gold", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wood_rate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("food_rate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("iron_rate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gold_rate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("buildings", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_updated", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("wood >= 0", name="ck_city_wood_nonneg"),
        sa.CheckConstraint("food >= 0", name="ck_city_food_nonneg"),
        sa.CheckConstraint("iron >= 0", name="ck_city_iron_nonneg"),
        sa.CheckConstraint("gold >= 0", name="ck_city_gold_nonneg"),
        sa.UniqueConstraint("x", "y", name="uq_city_position"),
    )
    op.create_index("ix_cities_player_id", "cities", ["player_id"])

    op.create_table(
        "armies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("home_city_id", sa.Integer(), sa.ForeignKey("cities.id"), nullable=False),
        sa.Column("location_city_id", sa.Integer(), sa.ForeignKey("cities.id"), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("units", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_armies_player_id", "armies", ["player_id"])
    op.create_index("ix_armies_location_city_id", "armies", ["location_city_id"])

    op.create_table(
        "movements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("army_id", sa.Integer(), sa.ForeignKey("armies.id"), nullable=False),
        sa.Column("origin_city_id", sa.Integer(), sa.ForeignKey("cities.id"), nullable=True),
        sa.Column("destination_city_id", sa.Integer(), sa.ForeignKey("cities.id"), nullable=True),
        sa.Column("origin_x", sa.Float(), nullable=False),
        sa.Column("origin_y", sa.Float(), nullable=False),
        sa.Column("destination_x", sa.Float(), nullable=False),
        sa.Column("destination_y", sa.Float(), nullable=False),
        sa.Column("depart_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arrive_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mission", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("relocate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("loot_wood", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("loot_food", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("loot_iron", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("loot_gold", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cause_event_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_movements_army_id", "movements", ["army_id"])
    op.create_index(
        "uq_movement_one_active_per_army",
        "movements",
        ["army_id"],
        unique=True,
        postgresql_where=sa.text("status = 'in_progress'"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("movement_id", sa.Integer(), sa.ForeignKey("movements.id"), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=80), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_events_idempotency_key"),
    )
    op.create_index("ix_events_pending_due", "events", ["status", "due_at"])
    op.create_index("ix_events_movement_id", "events", ["movement_id"])

    op.create_table(
        "battle_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("movement_id", sa.Integer(), sa.ForeignKey("movements.id"), nullable=False),
        sa.Column("attacker_player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("defender_player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=False),
        sa.Column("attacker_army_id", sa.Integer(), sa.ForeignKey("armies.id"), nullable=False),
        sa.Column("defender_city_id", sa.Integer(), sa.ForeignKey("cities.id"), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("winner", sa.String(length=20), nullable=False),
        sa.Column("attacker_before", postgresql.JSONB(), nullable=False),
        sa.Column("defender_before", postgresql.JSONB(), nullable=False),
        sa.Column("attacker_remaining", postgresql.JSONB(), nullable=False),
        sa.Column("defender_remaining", postgresql.JSONB(), nullable=False),
        sa.Column("attacker_casualties", postgresql.JSONB(), nullable=False),
        sa.Column("defender_casualties", postgresql.JSONB(), nullable=False),
        sa.Column("defender_resources", postgresql.JSONB(), nullable=False),
        sa.Column("loot", postgresql.JSONB(), nullable=False),
        sa.Column("rounds", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", name="uq_battle_reports_event_id"),
    )
    op.create_index("ix_battle_reports_movement_id", "battle_reports", ["movement_id"])
    op.create_index("ix_battle_reports_attacker_player_id", "battle_reports", ["attacker_player_id"])
    op.create_index("ix_battle_reports_defender_player_id", "battle_reports", ["defender_player_id"])

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.id"), nullable=True),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("cities.id"), nullable=True),
        sa.Column("resource", sa.String(length=64), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("source_event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_transactions_idempotency_key"),
    )
    op.create_index("ix_transactions_player_id", "transactions", ["player_id"])
    op.create_index("ix_transactions_city_id", "transactions", ["city_id"])
    op.create_index("ix_transactions_source_event_id", "transactions", ["source_event_id"])


def downgrade() -> None:
    op.drop_table("transactions")
    op.drop_table("battle_reports")
    op.drop_table("events")
    op.drop_index("uq_movement_one_active_per_army", table_name="movements")
    op.drop_table("movements")
    op.drop_table("armies")
    op.drop_table("cities")
    op.drop_table("players")
    op.drop_table("world_state")
