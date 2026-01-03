# src/db.py
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

logger = logging.getLogger(__name__)

DATABASE_URL = (os.getenv("DATABASE_URL") or "sqlite:///./app.db").strip()

def _normalize_db_url(url: str) -> str:
    u = (url or "").strip()
    # Render иногда даёт postgres:// — это алиас, лучше привести к postgresql://
    if u.startswith("postgres://"):
        u = u.replace("postgres://", "postgresql://", 1)

    # Если Postgres и драйвер не указан — форсим psycopg v3
    if u.startswith("postgresql://") and "+psycopg" not in u:
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)

    return u

DATABASE_URL = _normalize_db_url(DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db() -> None:
    """
    Создаёт таблицы, если их ещё нет.
    Важно: это не миграции. Для прод-миграций лучше Alembic.
    """
    try:
        SQLModel.metadata.create_all(engine)
        logger.info("DB initialized successfully.")
    except Exception as e:
        # не валим сервис насмерть — чтобы API/бот могли жить даже при проблемах DB
        logger.exception("DB init failed (service will continue): %s", e)

@contextmanager
def get_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
