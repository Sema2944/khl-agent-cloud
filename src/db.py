# src/db.py
from __future__ import annotations

import os
import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

logger = logging.getLogger(__name__)

DATABASE_URL = (os.getenv("DATABASE_URL") or "sqlite:///./app.db").strip()

# 👉 Render Postgres требует sslmode=require
if DATABASE_URL.startswith("postgresql://"):
    if "sslmode=" not in DATABASE_URL:
        DATABASE_URL += "?sslmode=require"

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
        # ⚠️ ВАЖНО: не роняем сервис, даже если БД временно недоступна
        logger.exception("DB init failed (service will continue): %s", e)

@contextmanager
def get_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
