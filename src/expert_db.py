# src/expert_db.py
import datetime as dt
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

    # ВАЖНО: используем dt.date, чтобы избежать проблем Pydantic v2
    date: dt.date = Field(index=True)

    text: str

    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

    updated_by: Optional[int] = Field(default=None, index=True)
