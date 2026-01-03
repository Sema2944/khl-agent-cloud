# src/db.py
from __future__ import annotations

import logging
import os
from typing import Generator

from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

logger = logging.getLogger(__name__)

DATABASE_URL = (os.getenv("DATABASE_URL") or "sqlite:///./app.db").strip()

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

def init_db() -> None:
    """
    Создаёт таблицы, если их нет.
    Важно: если таблицы уже есть со старой схемой — это НЕ миграция.
    """
    try:
        SQLModel.metadata.create_all(engine)
        logger.info("DB initialized successfully.")
    except Exception as e:
        logger.exception("DB init failed (service will continue): %s", e)

def get_session() -> Generator[Session, None, None]:
    """
    Правильный dependency-стиль для FastAPI:
    yield Session(...)
    """
    with Session(engine) as session:
        yield session
