# src/user_store.py
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from .db import engine
from .models import User

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.utcnow()


def get_or_create_user(tg_user_id: int, session: Optional[Session] = None) -> User:
    """
    Универсальная функция:
    - если session передали -> используем её
    - если session не передали (например, Telegram handler) -> открываем сами
    """
    if session is None:
        with Session(engine) as s:
            return get_or_create_user(tg_user_id, session=s)

    # ищем по tg_user_id
    u = session.exec(select(User).where(User.tg_user_id == int(tg_user_id))).first()
    if u:
        # touch updated_at
        u.updated_at = _now_utc()
        session.add(u)
        session.commit()
        session.refresh(u)
        return u

    u = User(
        tg_user_id=int(tg_user_id),
        created_at=_now_utc(),
        updated_at=_now_utc(),
    )
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def get_user_by_tg_id(tg_user_id: int, session: Optional[Session] = None) -> Optional[User]:
    if session is None:
        with Session(engine) as s:
            return get_user_by_tg_id(tg_user_id, session=s)

    return session.exec(select(User).where(User.tg_user_id == int(tg_user_id))).first()


def set_user_premium(tg_user_id: int, is_premium: bool, session: Optional[Session] = None) -> User:
    if session is None:
        with Session(engine) as s:
            return set_user_premium(tg_user_id, is_premium, session=s)

    u = get_or_create_user(tg_user_id, session=session)
    u.is_premium = bool(is_premium)
    u.updated_at = _now_utc()
    session.add(u)
    session.commit()
    session.refresh(u)
    return u
