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
        class _DummyCtx:
            def __enter__(self):  # noqa: D401
                return session

            def __exit__(self, exc_type, exc, tb):  # noqa: D401
                return False

        return _DummyCtx()

    # Session(engine) в sqlmodel поддерживает контекст-менеджер
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
    ВАЖНО (архитектура без переписываний):
    - Поддерживаем legacy схему, где раньше users.id == tg_user_id (и tg_user_id колонки не было).
    - Теперь используем tg_user_id как primary идентификатор Telegram.
    - Если нашли legacy-строку по id==tg_user_id -> привязываем tg_user_id и НЕ создаём дубликат.
    - Если создаём нового -> задаём id=tg_user_id, чтобы bets/bank (если где-то завязаны) не разъехались.
    """
    tg_user_id = int(tg_user_id)

    with _with_session(session) as s:
        # 1) Новый путь: ищем по tg_user_id (основной)
        u = s.exec(select(User).where(User.tg_user_id == tg_user_id)).first()
        if u:
            _update_user_profile(
                s,
                u,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            return u

        # 2) Legacy путь: раньше могли хранить tg id в users.id
        legacy = s.exec(select(User).where(User.id == tg_user_id)).first()
        if legacy:
            # если legacy-строка ещё не привязана — привязываем
            if legacy.tg_user_id is None:
                legacy.tg_user_id = tg_user_id

            _update_user_profile(
                s,
                legacy,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            return legacy

        # 3) Совсем новый пользователь: создаём запись
        # Ключевая фишка: id=tg_user_id, чтобы сохранить совместимость с bets_db
        u = User(
            id=tg_user_id,
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


def _update_user_profile(
    s: Session,
    u: User,
    *,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
) -> None:
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


def get_user_by_tg_id(tg_user_id: int, *, session: Optional[Session] = None) -> Optional[User]:
    tg_user_id = int(tg_user_id)
    with _with_session(session) as s:
        # основной путь
        u = s.exec(select(User).where(User.tg_user_id == tg_user_id)).first()
        if u:
            return u
        # legacy fallback
        return s.exec(select(User).where(User.id == tg_user_id)).first()


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


def get_user_seen_intro(tg_user_id: int) -> bool:
    """Check if user has seen onboarding intro."""
    try:
        with Session(engine) as s:
            u = get_or_create_user(int(tg_user_id), session=s)
            return bool(getattr(u, 'user_seen_intro', False))
    except Exception:
        logger.exception("get_user_seen_intro failed")
        return True  # fallback: skip onboarding on DB error


def set_user_seen_intro(tg_user_id: int, **profile) -> None:
    """Mark user as having seen onboarding. Also starts 3-day trial."""
    try:
        with Session(engine) as s:
            u = get_or_create_user(int(tg_user_id), session=s, **profile)
            u.user_seen_intro = True
            u.updated_at = _now()
            if getattr(u, 'trial_started_at', None) is None:
                u.trial_started_at = _now()
            s.add(u)
            s.commit()
    except Exception:
        logger.exception("set_user_seen_intro failed")


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
            select(Entitlement).where(
                Entitlement.user_id == int(user_id),
                Entitlement.feature == feature,
            )
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
            select(Entitlement).where(
                Entitlement.user_id == int(user_id),
                Entitlement.feature == feature,
            )
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
