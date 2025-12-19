# src/db.py
import os
from typing import Generator

from sqlmodel import SQLModel, create_engine, Session

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bets.db")

engine = create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    # Важно: импортируем модели, чтобы они зарегистрировались в metadata
    from .bets_db import User, Bet  # noqa: F401
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    """
    FastAPI dependency:
      with Depends(get_session) as session
    """
    with Session(engine) as session:
        yield session
