# src/expert_db.py
from __future__ import annotations

from datetime import datetime, date
from typing import Optional

from sqlmodel import SQLModel, Field


class ExpertStrategy(SQLModel, table=True):
    """
    Таблица стратегий эксперта.

    date       — на какую дату стратегия (YYYY-MM-DD)
    text       — текст стратегии
    updated_by — кто обновил (telegram user_id)
    updated_at — когда обновили
    """
    __tablename__ = "expert_strategy"

    id: Optional[int] = Field(default=None, primary_key=True)
    date: date = Field(index=True)
    text: str

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    updated_by: Optional[int] = Field(default=None, index=True)
