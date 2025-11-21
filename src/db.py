# src/db.py

import os
from datetime import datetime

from sqlmodel import SQLModel, create_engine, Session, Field

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bets.db")

engine = create_engine(DATABASE_URL, echo=False)


class User(SQLModel, table=True):
    """
    Простая модель пользователя.

    id            — Telegram ID пользователя
    bank          — текущий банк (может быть None, если ещё не задан)
    premium_until — дата/время (UTC), до которой активен премиум, либо None
    """
    id: int = Field(primary_key=True)
    bank: float | None = None
    premium_until: datetime | None = None


def init_db() -> None:
    # импорт моделей, чтобы они зарегистрировались в metadata
    from . import bets_db  # noqa: F401
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    """
    Функция, которую импортирует FastAPI (service.py).
    """
    return Session(engine)
