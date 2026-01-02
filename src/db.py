# src/db.py
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Render обычно даёт DATABASE_URL вида:
# - postgres://user:pass@host:port/db
# - postgresql://user:pass@host:port/db
# Нам нужен драйвер psycopg v3:
# - postgresql+psycopg://user:pass@host:port/db
DATABASE_URL = (os.getenv("DATABASE_URL") or "sqlite:///./app.db").strip()

def _normalize_db_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return "sqlite:///./app.db"

    # Render legacy scheme
    if u.startswith("postgres://"):
        u = u.replace("postgres://", "postgresql://", 1)

    # If it is postgres and no explicit driver -> force psycopg v3
    if u.startswith("postgresql://") and "+psycopg" not in u:
        u = u.replace("postgresql://", "postgresql+psycopg://", 1)

    return u

DATABASE_URL = _normalize_db_url(DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db() -> None:
    # Если используешь SQLModel — раскомментируй:
    # from sqlmodel import SQLModel
    # SQLModel.metadata.create_all(engine)
    return

@contextmanager
def get_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
