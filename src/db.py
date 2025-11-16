# src/db.py

from sqlmodel import SQLModel, create_engine, Session
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./bets.db")

engine = create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    # импорт моделей, чтобы они зарегистрировались в metadata
    from . import bets_db  # noqa: F401
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    """
    Функция, которую импортирует FastAPI (service.py).
    """
    return Session(engine)
