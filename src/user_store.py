# src/user_store.py
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from .db import get_session
from .models import User


def get_or_create_user(tg_user_id: int) -> User:
    with get_session() as s:  # type: ignore[var-annotated]
        assert isinstance(s, Session)
        u = s.exec(select(User).where(User.tg_user_id == tg_user_id)).first()
        if u:
            return u

        u = User(tg_user_id=tg_user_id)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u


def get_user(tg_user_id: int) -> User:
    return get_or_create_user(tg_user_id)


def mark_trial_used(tg_user_id: int) -> None:
    with get_session() as s:  # type: ignore[var-annotated]
        assert isinstance(s, Session)
        u = s.exec(select(User).where(User.tg_user_id == tg_user_id)).first()
        if not u:
            u = User(tg_user_id=tg_user_id)

        u.trial_live_used = True
        u.updated_at = datetime.utcnow()
        s.add(u)
        s.commit()


def set_premium(tg_user_id: int, *, is_premium: bool, premium_until: Optional[datetime] = None) -> None:
    with get_session() as s:  # type: ignore[var-annotated]
        assert isinstance(s, Session)
        u = s.exec(select(User).where(User.tg_user_id == tg_user_id)).first()
        if not u:
            u = User(tg_user_id=tg_user_id)

        u.is_premium = bool(is_premium)
        u.premium_until = premium_until
        u.updated_at = datetime.utcnow()
        s.add(u)
        s.commit()
