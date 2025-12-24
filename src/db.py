# src/db.py
from __future__ import annotations

import os
from typing import Generator

from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bets.db")

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

def init_db() -> None:
    """
    Регистрируем ВСЕ модели таблиц одним способом, чтобы не было дублей.
    """
    from . import bets_db      # noqa: F401
    from . import expert_db    # noqa: F401

    SQLModel.metadata.create_all(engine)

def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
