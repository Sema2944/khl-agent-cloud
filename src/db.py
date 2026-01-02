# src/db.py
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = (os.getenv("DATABASE_URL") or "sqlite:///./app.db").strip()

# Render может давать postgres:// — SQLAlchemy любит postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
