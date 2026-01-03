# src/entitlements.py
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sqlmodel import Session

from .db import engine
from .user_store import get_or_create_user, get_usage

logger = logging.getLogger(__name__)


@dataclass
class EffectiveEntitlements:
    """
    Единая модель прав/лимитов — чтобы потом не переписывать код под оплату.
    """
    tier: str  # "free" | "premium"

    is_premium: bool
    can_live: bool
    can_live_refresh: bool

    ai_daily_limit: int
    daily_ai_left: int

    live_refresh_daily_limit: int
    live_refresh_left: int

    live_min_interval_sec: float


def _today() -> str:
    return date.today().isoformat()


def _now() -> datetime:
    return datetime.utcnow()


def _env_int(key: str, default: int) -> int:
    v = (os.getenv(key) or "").strip()
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        logger.warning("Bad int ENV %s=%r, fallback=%s", key, v, default)
        return default


def _env_float(key: str, default: float) -> float:
    v = (os.getenv(key) or "").strip()
    if not v:
        return default
    try:
        return float(v)
    except Exception:
        logger.warning("Bad float ENV %s=%r, fallback=%s", key, v, default)
        return default


def _is_premium_active(user) -> bool:
    """
    Premium считается активным если:
    - user.is_premium == True
    - и premium_until либо None (вечный), либо в будущем
    """
    if not bool(getattr(user, "is_premium", False)):
        return False

    until = getattr(user, "premium_until", None)
    if until is None:
        return True

    try:
        return until > _now()
    except Exception:
        return False


def get_effective_entitlements(
    tg_user_id: int,
    *,
    session: Optional[Session] = None,
) -> EffectiveEntitlements:
    """
    Политика (MVP, но расширяемая):
    - PREMIUM: полный доступ (с учётом premium_until).
    - FREE:
        - LIVE: 1 trial (пока user.trial_live_used == False)
        - LIVE refresh: лимит N/день
        - AI pre (и всё остальное): лимит M/день

    Лимиты можно менять через ENV без переписывания кода:
      FREE_AI_DAILY_LIMIT=10
      FREE_LIVE_REFRESH_DAILY_LIMIT=3
      FREE_LIVE_MIN_INTERVAL_SEC=8
      PREMIUM_LIVE_MIN_INTERVAL_SEC=1
    """
    tg_user_id = int(tg_user_id)

    # лимиты по умолчанию (продуктовые ручки)
    FREE_AI_DAILY_LIMIT = _env_int("FREE_AI_DAILY_LIMIT", 10)
    FREE_LIVE_REFRESH_DAILY_LIMIT = _env_int("FREE_LIVE_REFRESH_DAILY_LIMIT", 3)
    FREE_LIVE_MIN_INTERVAL_SEC = _env_float("FREE_LIVE_MIN_INTERVAL_SEC", 8.0)

    PREMIUM_LIVE_MIN_INTERVAL_SEC = _env_float("PREMIUM_LIVE_MIN_INTERVAL_SEC", 1.0)

    # если session не передали — откроем сами
    if session is None:
        with Session(engine) as s:
            return get_effective_entitlements(tg_user_id, session=s)

    # user должен существовать
    u = get_or_create_user(tg_user_id, session=session)

    # подстраховка (на случай странностей БД/миграций)
    if getattr(u, "id", None) is None:
        logger.warning("User has no id after get_or_create_user (tg_user_id=%s). Using safe defaults.", tg_user_id)
        return EffectiveEntitlements(
            tier="free",
            is_premium=False,
            can_live=True,              # не ломаем UX
            can_live_refresh=True,
            ai_daily_limit=FREE_AI_DAILY_LIMIT,
            daily_ai_left=FREE_AI_DAILY_LIMIT,
            live_refresh_daily_limit=FREE_LIVE_REFRESH_DAILY_LIMIT,
            live_refresh_left=FREE_LIVE_REFRESH_DAILY_LIMIT,
            live_min_interval_sec=FREE_LIVE_MIN_INTERVAL_SEC,
        )

    # premium?
    if _is_premium_active(u):
        return EffectiveEntitlements(
            tier="premium",
            is_premium=True,
            can_live=True,
            can_live_refresh=True,
            ai_daily_limit=10**9,
            daily_ai_left=10**9,
            live_refresh_daily_limit=10**9,
            live_refresh_left=10**9,
            live_min_interval_sec=PREMIUM_LIVE_MIN_INTERVAL_SEC,
        )

    # free — считаем лимиты
    period = _today()

    used_ai = get_usage(int(u.id), "ai_pre", period=period, session=session)
    daily_ai_left = max(0, FREE_AI_DAILY_LIMIT - int(used_ai))

    used_refresh = get_usage(int(u.id), "live_refresh", period=period, session=session)
    live_refresh_left = max(0, FREE_LIVE_REFRESH_DAILY_LIMIT - int(used_refresh))

    # LIVE доступ: 1 trial
    can_live = not bool(getattr(u, "trial_live_used", False))
    can_live_refresh = live_refresh_left > 0

    return EffectiveEntitlements(
        tier="free",
        is_premium=False,
        can_live=can_live,
        can_live_refresh=can_live_refresh,
        ai_daily_limit=FREE_AI_DAILY_LIMIT,
        daily_ai_left=daily_ai_left,
        live_refresh_daily_limit=FREE_LIVE_REFRESH_DAILY_LIMIT,
        live_refresh_left=live_refresh_left,
        live_min_interval_sec=FREE_LIVE_MIN_INTERVAL_SEC,
    )
