# src/db.py
from __future__ import annotations

import os
import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

DATABASE_URL = (os.getenv("DATABASE_URL") or "sqlite:///./app.db").strip()

def _normalize_database_url(url: str) -> str:
    u = (url or "").strip()

    # Render часто даёт postgres:// (устаревший алиас) — приводим
    if u.startswith("postgres://"):
        u = u.replace("postgres://", "postgresql://", 1)

    # Если Postgres и драйвер не указан — ставим psycopg v3
    if u.startswith("postgresql://") and "+psycopg" not in u:
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)

    return u

DATABASE_URL = _normalize_database_url(DATABASE_URL)

# Для SQLite нужны connect_args, для Postgres — нет
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# Быстрый фейл с понятным текстом, если psycopg не установлен
if DATABASE_URL.startswith("postgresql+psycopg://"):
    try:
        import psycopg  # noqa: F401
    except Exception as e:
        raise RuntimeError(
            "Postgres URL uses psycopg driver (postgresql+psycopg://), "
            "but package 'psycopg' is not installed. "
            "Add: psycopg[binary]==3.2.3 to requirements.txt"
        ) from e

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db() -> None:
    """
    Если используешь SQLModel — раскомментируй:
    from sqlmodel import SQLModel
    SQLModel.metadata.create_all(engine)
    """
    return

@contextmanager
def get_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
