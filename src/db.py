# src/db.py
from __future__ import annotations

import os
import logging
from contextlib import contextmanager
from typing import Generator
from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

logger = logging.getLogger(__name__)

DATABASE_URL = (os.getenv("DATABASE_URL") or "sqlite:///./app.db").strip()

def _normalize_db_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "sqlite:///./app.db"

    # SQLite
    if url.startswith("sqlite"):
        return url

    # Render / Postgres: часто дают postgresql://
    # Нам нужен psycopg v3: postgresql+psycopg://
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    # sslmode=require (для Render Postgres)
    if url.startswith("postgresql+psycopg://") and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"

    return url

DATABASE_URL = _normalize_db_url(DATABASE_URL)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

def init_db() -> None:
    try:
        SQLModel.metadata.create_all(engine)
        logger.info("DB initialized successfully.")
    except Exception as e:
        # не роняем сервис из-за БД
        logger.exception("DB init failed (service will continue): %s", e)

@contextmanager
def get_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
