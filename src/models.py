# src/models.py
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import SQLModel, Field


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tg_user_id", name="uq_users_tg_user_id"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Telegram
    tg_user_id: int = Field(index=True, nullable=False)

    # Billing / access
    is_premium: bool = Field(default=False, nullable=False)
    premium_until: Optional[datetime] = Field(default=None, nullable=True)

    # FREE -> 1 trial LIVE
    trial_live_used: bool = Field(default=False, nullable=False)

    # Counters / limits (на будущее)
    free_llm_calls_today: int = Field(default=0, nullable=False)
    free_llm_day: Optional[str] = Field(default=None, nullable=True)  # YYYY-MM-DD

    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

    @property
    def can_live(self) -> bool:
        # Premium всегда может
        if self.is_premium:
            return True
        # Free может только если trial не использован
        return not self.trial_live_used
