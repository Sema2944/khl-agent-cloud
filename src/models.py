# src/models.py
from __future__ import annotations

from datetime import datetime, date
from typing import Optional

from sqlmodel import SQLModel, Field, UniqueConstraint


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tg_user_id", name="uq_users_tg_user_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)

    tg_user_id: int = Field(index=True)

    # тариф/доступ
    is_premium: bool = Field(default=False, index=True)

    # trial по LIVE (чтобы 1 раз показать бесплатно)
    trial_live_used: bool = Field(default=False, index=True)

    # банк / профиль
    bank: float = Field(default=0.0)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Subscription(SQLModel, table=True):
    """
    Для платежей/подписок (YooKassa/Telegram Payments):
    можно хранить провайдера, статус, дату окончания периода.
    """
    __tablename__ = "subscriptions"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)

    provider: str = Field(default="telegram", index=True)   # telegram | yookassa | manual
    status: str = Field(default="inactive", index=True)     # active | canceled | inactive
    current_period_end: Optional[datetime] = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Entitlement(SQLModel, table=True):
    """
    Гибкие права (на будущее):
    - key: 'live_access', 'daily_ai', 'export' и т.д.
    - value_int: например лимит/кол-во
    - value_bool: флаг
    - expires_at: срок действия
    """
    __tablename__ = "entitlements"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)

    key: str = Field(index=True)
    value_int: int = Field(default=0)
    value_bool: bool = Field(default=False)
    expires_at: Optional[datetime] = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)


class UsageCounter(SQLModel, table=True):
    """
    Лимиты free-тарифа:
    например:
      key='live_refresh' count=3 per day
      key='ai_pre' count=10 per day
    """
    __tablename__ = "usage_counters"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)

    key: str = Field(index=True)
    period_start: date = Field(index=True)  # обычно текущая дата (day bucket)
    count: int = Field(default=0)

    updated_at: datetime = Field(default_factory=datetime.utcnow)
