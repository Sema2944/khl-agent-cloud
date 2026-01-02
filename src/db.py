# src/db.py
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

DATABASE_URL = (os.getenv("DATABASE_URL") or "sqlite:///./app.db").strip()

# На Render Postgres обычно приходит как postgresql://...
# Для psycopg v3 нужен postgresql+psycopg://
if DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    # Важно: импорт моделей ДО create_all, чтобы SQLModel увидел таблицы
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
