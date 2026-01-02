# src/models.py
from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Dict, Any

from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON, UniqueConstraint


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    tg_user_id: int = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # удобный флаг (но НЕ источник истины)
    tier: str = Field(default="free", index=True)  # "free" | "premium"

    __table_args__ = (UniqueConstraint("tg_user_id", name="uq_users_tg_user_id"),)


class Subscription(SQLModel, table=True):
    __tablename__ = "subscriptions"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="users.id")

    provider: str = Field(default="manual", index=True)  # telegram | yookassa | manual
    plan_code: str = Field(default="premium_month", index=True)

    status: str = Field(default="inactive", index=True)  # active | inactive | canceled | expired | past_due
    current_period_end: Optional[datetime] = Field(default=None, index=True)

    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Entitlement(SQLModel, table=True):
    """
    Эффективные права/лимиты. Это то, на что опирается бизнес-логика.
    Хочешь новый тариф/акцию — меняешь entitlements, код почти не трогаешь.
    """
    __tablename__ = "entitlements"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="users.id")

    # лимиты
    ai_daily_limit: int = Field(default=10)
    live_min_interval_sec: int = Field(default=30)  # минимальная пауза между LIVE-обновлениями

    # фичи (просто включатели)
    features: Dict[str, Any] = Field(sa_column=Column(JSON), default_factory=dict)

    effective_from: datetime = Field(default_factory=datetime.utcnow, index=True)
    effective_to: Optional[datetime] = Field(default=None, index=True)


class UsageCounter(SQLModel, table=True):
    """
    Учет лимитов по дням.
    """
    __tablename__ = "usage_counters"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="users.id")
    day: date = Field(index=True)

    ai_calls: int = Field(default=0)
    live_calls: int = Field(default=0)

    updated_at: datetime = Field(default_factory=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_usage_user_day"),)
