# src/db.py
from __future__ import annotations

import os
from datetime import datetime
from typing import Generator

from sqlmodel import SQLModel, create_engine, Session, Field

# ---------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bets.db")

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite")
    else {},
)


# ---------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------

class User(SQLModel, table=True):
    """
    Модель пользователя.

    id            — Telegram ID пользователя
    bank          — текущий банк (может быть None)
    premium_until — дата/время (UTC), до которой активен премиум
    """
    id: int = Field(primary_key=True)
    bank: float | None = None
    premium_until: datetime | None = None


# ---------------------------------------------------------------------
# INIT DB
# ---------------------------------------------------------------------

def init_db() -> None:
    """
    Инициализация БД.
    Важно: импортируем bets_db, чтобы таблицы Bet зарегистрировались.
    """
    from . import bets_db  # noqa: F401

    SQLModel.metadata.create_all(engine)


# ---------------------------------------------------------------------
# SESSION (FastAPI dependency)
# ---------------------------------------------------------------------

def get_session() -> Generator[Session, None, None]:
    """
    Dependency для FastAPI.
    Корректно открывает и закрывает SQLAlchemy Session.
    """
    with Session(engine) as session:
        yield session
