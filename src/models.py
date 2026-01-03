# src/models.py
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint


class User(SQLModel, table=True):
    """
    Единая таблица пользователей.
    ВАЖНО: extend_existing=True защищает от повторной регистрации таблицы
    при двойном импорте модулей (частая история на Render + FastAPI + Telegram).
    """
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tg_user_id", name="uq_users_tg_user_id"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    tg_user_id: int = Field(index=True)
    username: Optional[str] = Field(default=None)
    first_name: Optional[str] = Field(default=None)
    last_name: Optional[str] = Field(default=None)

    # тариф / доступ
    is_premium: bool = Field(default=False, index=True)
    premium_until: Optional[datetime] = Field(default=None, index=True)

    # trial для LIVE (например: 1 раз бесплатный live)
    trial_live_used: bool = Field(default=False)

    # служебное
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class Subscription(SQLModel, table=True):
    """
    Подписки (на будущее: YooKassa/Telegram Payments)
    """
    __tablename__ = "subscriptions"
    __table_args__ = ({"extend_existing": True},)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)

    provider: str = Field(default="telegram")  # telegram | yookassa | manual
    status: str = Field(default="active", index=True)  # active|canceled|expired|pending

    started_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    expires_at: Optional[datetime] = Field(default=None, index=True)

    # идентификаторы платежей (опционально)
    payment_id: Optional[str] = Field(default=None, index=True)
    invoice_payload: Optional[str] = Field(default=None, index=True)


class Entitlement(SQLModel, table=True):
    """
    Права/фичи (free/premium) — гибко, чтобы потом не переписывать.
    Пример: feature='live', allowed=True
    """
    __tablename__ = "entitlements"
    __table_args__ = (
        UniqueConstraint("user_id", "feature", name="uq_entitlements_user_feature"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)

    feature: str = Field(index=True)  # live | prematch | expert | etc
    allowed: bool = Field(default=True)

    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class UsageCounter(SQLModel, table=True):
    """
    Счётчики использования (лимиты для FREE).
    Например: key='live_requests_day', period='2026-01-03', count=3
    """
    __tablename__ = "usage_counters"
    __table_args__ = (
        UniqueConstraint("user_id", "key", "period", name="uq_usage_user_key_period"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)

    key: str = Field(index=True)          # live_requests_day, ai_requests_day, ...
    period: str = Field(index=True)       # 'YYYY-MM-DD' или 'YYYY-MM'
    count: int = Field(default=0)

    updated_at: datetime = Field(default_factory=datetime.utcnow, index=True)
