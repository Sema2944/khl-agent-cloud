# src/db.py
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

DATABASE_URL = (os.getenv("DATABASE_URL") or "sqlite:///./app.db").strip()

# --- Normalize Postgres URL to psycopg v3 driver ---
# Render обычно даёт postgres://... (или postgresql://...)
# SQLAlchemy 2 + psycopg3: postgresql+psycopg://...
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """
    Создаёт таблицы (если используешь SQLModel).
    Важно: НЕ валим весь сервис, если Postgres временно недоступен/не привязан.
    """
    try:
        from sqlmodel import SQLModel
        SQLModel.metadata.create_all(engine)
        logger.info("DB init: create_all OK")
    except Exception as e:
        logger.exception("DB init failed (service will continue): %s", e)


@contextmanager
def get_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
