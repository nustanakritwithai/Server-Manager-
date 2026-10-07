from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class WorldState(Base):
    """Singleton row (id = 1). offset_seconds is added to the process clock.

    world_version increments when the simulation changes. commands_open,
    worker_paused, and restore_active are operational gates used by snapshot
    restore; they are not part of the world checksum.
    """

    __tablename__ = "world_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offset_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    world_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    commands_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    worker_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    restore_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    research: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class City(Base):
    __tablename__ = "cities"
    __table_args__ = (
        CheckConstraint("wood >= 0", name="ck_city_wood_nonneg"),
        CheckConstraint("food >= 0", name="ck_city_food_nonneg"),
        CheckConstraint("iron >= 0", name="ck_city_iron_nonneg"),
        CheckConstraint("gold >= 0", name="ck_city_gold_nonneg"),
        Index("uq_city_position", "x", "y", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    x: Mapped[int] = mapped_column(Integer, nullable=False)
    y: Mapped[int] = mapped_column(Integer, nullable=False)
    wood: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    food: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    iron: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wood_rate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    food_rate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    iron_rate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gold_rate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    buildings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Army(Base):
    __tablename__ = "armies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    home_city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"), nullable=False)
    location_city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    units: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Movement(Base):
    """One leg of a journey. origin/destination are stored as city ids plus coordinates.

    Coordinates are floats because a recall can start from a point along the path.
    """

    __tablename__ = "movements"
    __table_args__ = (
        Index(
            "uq_movement_one_active_per_army",
            "army_id",
            unique=True,
            postgresql_where=text("status = 'in_progress'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    army_id: Mapped[int] = mapped_column(ForeignKey("armies.id"), nullable=False, index=True)
    origin_city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"), nullable=True)
    destination_city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"), nullable=True)
    origin_x: Mapped[float] = mapped_column(Float, nullable=False)
    origin_y: Mapped[float] = mapped_column(Float, nullable=False)
    destination_x: Mapped[float] = mapped_column(Float, nullable=False)
    destination_y: Mapped[float] = mapped_column(Float, nullable=False)
    depart_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    arrive_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mission: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    relocate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    loot_wood: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    loot_food: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    loot_iron: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    loot_gold: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cause_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_pending_due", "status", "due_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    movement_id: Mapped[int | None] = mapped_column(ForeignKey("movements.id"), nullable=True, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BattleReport(Base):
    __tablename__ = "battle_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False, unique=True)
    movement_id: Mapped[int] = mapped_column(ForeignKey("movements.id"), nullable=False, index=True)
    attacker_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    defender_player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False, index=True)
    attacker_army_id: Mapped[int] = mapped_column(ForeignKey("armies.id"), nullable=False)
    defender_city_id: Mapped[int] = mapped_column(ForeignKey("cities.id"), nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    winner: Mapped[str] = mapped_column(String(20), nullable=False)
    attacker_before: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    defender_before: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    attacker_remaining: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    defender_remaining: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    attacker_casualties: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    defender_casualties: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    defender_resources: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    loot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rounds: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Transaction(Base):
    """Append-only resource ledger. idempotency_key makes a retried event a no-op."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id"), nullable=True, index=True)
    city_id: Mapped[int | None] = mapped_column(ForeignKey("cities.id"), nullable=True, index=True)
    resource: Mapped[str] = mapped_column(String(64), nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    source_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorldSnapshot(Base):
    """Metadata for one captured world. The payload lives in world_snapshot_payloads.

    A snapshot is a simulation rollback point. It is not a database backup.
    """

    __tablename__ = "world_snapshots"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('AUTO', 'MANUAL', 'SAFETY')",
            name="ck_snapshot_reason",
        ),
        CheckConstraint(
            "status IN ('CREATING', 'READY', 'FAILED', 'RESTORING')",
            name="ck_snapshot_status",
        ),
        Index("ix_world_snapshots_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    world_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    world_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorldSnapshotPayload(Base):
    """Canonical JSON document whose SHA-256 is world_snapshots.checksum."""

    __tablename__ = "world_snapshot_payloads"

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("world_snapshots.id", ondelete="CASCADE"),
        primary_key=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
