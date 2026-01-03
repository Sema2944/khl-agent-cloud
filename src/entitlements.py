# src/entitlements.py
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from .db import engine
from .models import User, UsageCounter

# -----------------------------
# ENV limits (меняются без кода)
# -----------------------------
FREE_DAILY_AI_PRE_LIMIT = int((os.getenv("FREE_DAILY_AI_PRE_LIMIT") or "10").strip())
FREE_DAILY_LIVE_REFRESH_LIMIT = int((os.getenv("FREE_DAILY_LIVE_REFRESH_LIMIT") or "3").strip())

# ключи usage
USAGE_AI_PRE = "ai_pre"
USAGE_LIVE_REFRESH = "live_refresh"


@dataclass
class EffectiveEntitlements:
    tier: str                 # "free" | "premium"
    is_premium: bool
    can_live: bool
    can_live_refresh: bool
    ai_daily_limit: int
    daily_ai_left: int
    live_refresh_daily_limit: int
    live_refresh_left: int
    live_min_interval_sec: int  # на будущее (UI/бот может показывать)


def _today_period() -> str:
    # UsageCounter.period у тебя строка — делаем YYYY-MM-DD
    return date.today().isoformat()


def _get_usage(session: Session, user_id: int, key: str, period: str) -> int:
    row = session.exec(
        select(UsageCounter).where(
            UsageCounter.user_id == int(user_id),
            UsageCounter.key == key,
            UsageCounter.period == period,
        )
    ).first()
    return int(row.count) if row else 0


def get_effective_entitlements(
    tg_user_id: int,
    *,
    session: Optional[Session] = None,
) -> EffectiveEntitlements:
    """
    Единственная функция для определения доступа.

    Premium:
      - безлимит: live, refresh, ai_pre

    Free:
      - live: 1 trial (trial_live_used=False)
      - refresh: лимит/день (FREE_DAILY_LIVE_REFRESH_LIMIT)
      - ai_pre: лимит/день (FREE_DAILY_AI_PRE_LIMIT)

    Важно: session опционален, чтобы одинаково работало из Telegram и API.
    """
    close_session = False
    if session is None:
        session = Session(engine)
        close_session = True

    try:
        user = session.exec(select(User).where(User.tg_user_id == int(tg_user_id))).first()
        if user is None:
            # создаём минимального юзера
            user = User(tg_user_id=int(tg_user_id))
            session.add(user)
            session.commit()
            session.refresh(user)

        # -----------------------------
        # PREMIUM
        # -----------------------------
        if bool(user.is_premium):
            return EffectiveEntitlements(
                tier="premium",
                is_premium=True,
                can_live=True,
                can_live_refresh=True,
                ai_daily_limit=10**9,
                daily_ai_left=10**9,
                live_refresh_daily_limit=10**9,
                live_refresh_left=10**9,
                live_min_interval_sec=0,
            )

        # -----------------------------
        # FREE
        # -----------------------------
        period = _today_period()

        used_ai = _get_usage(session, user.id, USAGE_AI_PRE, period)
        ai_left = max(0, int(FREE_DAILY_AI_PRE_LIMIT) - int(used_ai))

        used_refresh = _get_usage(session, user.id, USAGE_LIVE_REFRESH, period)
        refresh_left = max(0, int(FREE_DAILY_LIVE_REFRESH_LIMIT) - int(used_refresh))

        # LIVE trial: 1 раз бесплатно
        can_live = not bool(user.trial_live_used)

        # refresh разрешаем только если осталось
        can_live_refresh = refresh_left > 0

        return EffectiveEntitlements(
            tier="free",
            is_premium=False,
            can_live=can_live,
            can_live_refresh=can_live_refresh,
            ai_daily_limit=int(FREE_DAILY_AI_PRE_LIMIT),
            daily_ai_left=int(ai_left),
            live_refresh_daily_limit=int(FREE_DAILY_LIVE_REFRESH_LIMIT),
            live_refresh_left=int(refresh_left),
            live_min_interval_sec=3,  # free обычно медленнее; можно потом вынести в ENV
        )

    finally:
        if close_session:
            session.close()
