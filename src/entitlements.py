# src/entitlements.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from .db import engine
from .models import User, UsageCounter
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


def get_effective_entitlements(
    tg_user_id: int,
    *,
    session: Optional[Session] = None,
) -> EffectiveEntitlements:
    """
    Политика (MVP, но расширяемая):
    - PREMIUM: полный доступ.
    - FREE:
        - LIVE: 1 trial (пока user.trial_live_used == False)
        - LIVE refresh: лимит N/день
        - AI pre (и всё остальное): лимит M/день
    """
    # лимиты по умолчанию (можно вынести в ENV позже)
    FREE_AI_DAILY_LIMIT = 10
    FREE_LIVE_REFRESH_DAILY_LIMIT = 3
    FREE_LIVE_MIN_INTERVAL_SEC = 8.0  # "анти-спам" для live кнопок

    PREMIUM_LIVE_MIN_INTERVAL_SEC = 1.0

    # если session не передали — откроем сами
    if session is None:
        with Session(engine) as s:
            return get_effective_entitlements(tg_user_id, session=s)

    # 1) user должен существовать
    u = get_or_create_user(int(tg_user_id), session=session)

    # 2) premium?
    if bool(u.is_premium):
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

    # 3) free — считаем лимиты
    period = _today()

    used_ai = get_usage(u.id, "ai_pre", period=period, session=session)
    daily_ai_left = max(0, FREE_AI_DAILY_LIMIT - used_ai)

    used_refresh = get_usage(u.id, "live_refresh", period=period, session=session)
    live_refresh_left = max(0, FREE_LIVE_REFRESH_DAILY_LIMIT - used_refresh)

    # LIVE доступ: 1 trial
    can_live = (not bool(u.trial_live_used))
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
