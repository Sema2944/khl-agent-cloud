# src/expert_db.py
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlmodel import SQLModel, Field, Session, select


class ExpertStrategy(SQLModel, table=True):
    """
    Таблица стратегий эксперта.

    date       — дата стратегии (dt.date)
    text       — текст стратегии
    updated_by — кто обновил (telegram user_id)
    created_at — когда создали
    updated_at — когда обновили
    """
    __tablename__ = "expert_strategy"
    __table_args__ = {"extend_existing": True}  # важно: гасит конфликт metadata

    id: Optional[int] = Field(default=None, primary_key=True)

    # ВАЖНО: dt.date (а не str), чтобы не ловить pydantic/sqlmodel проблемы
    date: dt.date = Field(index=True)

    text: str

    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

    updated_by: Optional[int] = Field(default=None, index=True)


def get_strategy(session: Session, day: dt.date) -> Optional[ExpertStrategy]:
    """
    Достаём стратегию на конкретную дату.
    Если несколько записей (не должно быть), берём самую свежую по updated_at.
    """
    stmt = (
        select(ExpertStrategy)
        .where(ExpertStrategy.date == day)
        .order_by(ExpertStrategy.updated_at.desc())
    )
    return session.exec(stmt).first()


def upsert_strategy(session: Session, day: dt.date, text: str, updated_by: Optional[int]) -> ExpertStrategy:
    """
    Обновить стратегию на дату, если есть, иначе создать.
    """
    row = get_strategy(session, day)
    now = dt.datetime.utcnow()

    if row is None:
        row = ExpertStrategy(
            date=day,
            text=text,
            created_at=now,
            updated_at=now,
            updated_by=updated_by,
        )
        session.add(row)
    else:
        row.text = text
        row.updated_at = now
        row.updated_by = updated_by
        session.add(row)

    session.commit()
    session.refresh(row)
    return row
