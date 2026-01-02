# src/user_store.py
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import select

from .db import get_session
from .models import User


def get_or_create_user(tg_user_id: int) -> User:
    with get_session() as s:
        u = s.exec(select(User).where(User.tg_user_id == tg_user_id)).first()
        if u:
            return u
        u = User(tg_user_id=tg_user_id, created_at=datetime.utcnow(), tier="free")
        s.add(u)
        s.commit()
        s.refresh(u)
        return u
