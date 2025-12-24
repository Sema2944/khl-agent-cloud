import datetime as dt
from typing import Optional

from sqlmodel import SQLModel, Field


class ExpertStrategy(SQLModel, table=True):
    __tablename__ = "expert_strategy"
    __table_args__ = {"extend_existing": True}  # <- защита от повторной регистрации

    id: Optional[int] = Field(default=None, primary_key=True)

    # дата стратегии (по МСК будет вычисляться в parsing.py)
    date: dt.date = Field(index=True)

    text: str

    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

    updated_by: Optional[int] = Field(default=None, index=True)
