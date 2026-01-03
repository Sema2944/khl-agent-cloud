# src/user_store.py
from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Optional

from sqlmodel import Session, select

from .db import engine
from .models import User, UsageCounter, Entitlement

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.utcnow()


def _today_str() -> str:
    return date.today().isoformat()


def _with_session(session: Optional[Session]):
    """
    Мини-хелпер: если session=None -> открываем свою.
    Использование:
      with _with_session(session) as s:
          ...
    """
    if session is not None:
        # прокидываем внешний session как контекст-менеджер-пустышку
        class _DummyCtx:
            def __enter__(self): return session
            def __exit__(self, exc_type, exc, tb): return False
        return _DummyCtx()
    return Session(engine)


# -----------------------------
# Users
# -----------------------------
def get_or_create_user(
    tg_user_id: int,
    *,
    session: Optional[Session] = None,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> User:
    """
    Всегда безопасно:
    - если session не передали (Telegram handlers) -> откроем сами
    - если передали (FastAPI Depends) -> используем её
    """
    tg_user_id = int(tg_user_id)

    with _with_session(session) as s:
        u = s.exec(select(User).where(User.tg_user_id == tg_user_id)).first()
        if u:
            # обновим профиль, если пришли данные
            changed = False
            if username is not None and username != u.username:
                u.username = username
                changed = True
            if first_name is not None and first_name != u.first_name:
                u.first_name = first_name
                changed = True
            if last_name is not None and last_name != u.last_name:
                u.last_name = last_name
                changed = True

            u.updated_at = _now()
            if changed:
                s.add(u)
            s.commit()
            s.refresh(u)
            return u

        u = User(
            tg_user_id=tg_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            created_at=_now(),
            updated_at=_now(),
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u


def get_user_by_tg_id(tg_user_id: int, *, session: Optional[Session] = None) -> Optional[User]:
    tg_user_id = int(tg_user_id)
    with _with_session(session) as s:
        return s.exec(select(User).where(User.tg_user_id == tg_user_id)).first()


def set_user_premium(
    tg_user_id: int,
    is_premium: bool,
    *,
    premium_until: Optional[datetime] = None,
    session: Optional[Session] = None,
) -> User:
    with _with_session(session) as s:
        u = get_or_create_user(int(tg_user_id), session=s)
        u.is_premium = bool(is_premium)
        u.premium_until = premium_until
        u.updated_at = _now()
        s.add(u)
        s.commit()
        s.refresh(u)
        return u


def mark_trial_live_used(tg_user_id: int, *, session: Optional[Session] = None) -> User:
    with _with_session(session) as s:
        u = get_or_create_user(int(tg_user_id), session=s)
        if not u.trial_live_used:
            u.trial_live_used = True
            u.updated_at = _now()
            s.add(u)
            s.commit()
            s.refresh(u)
        return u


# -----------------------------
# Entitlements (на будущее / гибко)
# -----------------------------
def set_entitlement(
    user_id: int,
    feature: str,
    allowed: bool,
    *,
    session: Optional[Session] = None,
) -> Entitlement:
    feature = (feature or "").strip().lower()

    with _with_session(session) as s:
        e = s.exec(
            select(Entitlement).where(Entitlement.user_id == int(user_id), Entitlement.feature == feature)
        ).first()

        if e:
            e.allowed = bool(allowed)
            e.updated_at = _now()
            s.add(e)
            s.commit()
            s.refresh(e)
            return e

        e = Entitlement(
            user_id=int(user_id),
            feature=feature,
            allowed=bool(allowed),
            created_at=_now(),
            updated_at=_now(),
        )
        s.add(e)
        s.commit()
        s.refresh(e)
        return e


def get_entitlement(user_id: int, feature: str, *, session: Optional[Session] = None) -> Optional[Entitlement]:
    feature = (feature or "").strip().lower()
    with _with_session(session) as s:
        return s.exec(
            select(Entitlement).where(Entitlement.user_id == int(user_id), Entitlement.feature == feature)
        ).first()


# -----------------------------
# Usage counters (лимиты FREE)
# -----------------------------
def inc_usage(
    user_id: int,
    key: str,
    *,
    period: Optional[str] = None,
    amount: int = 1,
    session: Optional[Session] = None,
) -> UsageCounter:
    key = (key or "").strip().lower()
    period = (period or _today_str()).strip()

    with _with_session(session) as s:
        row = s.exec(
            select(UsageCounter).where(
                UsageCounter.user_id == int(user_id),
                UsageCounter.key == key,
                UsageCounter.period == period,
            )
        ).first()

        if row:
            row.count = int(row.count or 0) + int(amount)
            row.updated_at = _now()
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

        row = UsageCounter(
            user_id=int(user_id),
            key=key,
            period=period,
            count=int(amount),
            updated_at=_now(),
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def get_usage(
    user_id: int,
    key: str,
    *,
    period: Optional[str] = None,
    session: Optional[Session] = None,
) -> int:
    key = (key or "").strip().lower()
    period = (period or _today_str()).strip()

    with _with_session(session) as s:
        row = s.exec(
            select(UsageCounter).where(
                UsageCounter.user_id == int(user_id),
                UsageCounter.key == key,
                UsageCounter.period == period,
            )
        ).first()
        return int(row.count) if row else 0
