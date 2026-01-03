# src/entitlements.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlmodel import Session

from .db import engine
from .user_store import (
    get_or_create_user,
    get_usage,
)

# -----------------------------
# Result model
# -----------------------------
@dataclass
class EffectiveEntitlements:
    tier: str                 # "free" | "premium"
    is_premium: bool

    # features
    can_live: bool
    can_live_refresh: bool

    # limits
    ai_daily_limit: int
    daily_ai_left: int
    live_min_interval_sec: int  # для UX (в будущем)


# -----------------------------
# Helpers
# -----------------------------
def _today_str() -> str:
    return date.today().isoformat()


def _with_session(session: Optional[Session]):
    """
    Если session=None -> открываем свою.
    """
    if session is not None:
        class _DummyCtx:
            def __enter__(self):  # noqa: D401
                return session
            def __exit__(self, exc_type, exc, tb):  # noqa: D401
                return False
        return _DummyCtx()

    return Session(engine)


# -----------------------------
# Public API
# -----------------------------
def get_effective_entitlements(
    tg_user_id: int,
    *,
    session: Optional[Session] = None,
) -> EffectiveEntitlements:
    """
    Единая политика доступа (free / premium), рассчитана так,
    чтобы потом не переписывать код при подключении подписок.

    Политика по умолчанию:
    - Premium:
        - полный доступ
        - лимиты "бесконечные"
    - Free:
        - pre-ai: дневной лимит (пример 10)
        - live: 1 trial (если trial_live_used=False)
        - live refresh: дневной лимит (пример 3)
        - live_min_interval_sec: минимальная пауза для UX (пример 25 сек)
    """
    tg_user_id = int(tg_user_id)
    today = _today_str()

    with _with_session(session) as s:
        # всегда гарантируем наличие user
        u = get_or_create_user(tg_user_id, session=s)

        # -----------------------------
        # PREMIUM
        # -----------------------------
        if bool(u.is_premium):
            return EffectiveEntitlements(
                tier="premium",
                is_premium=True,
                can_live=True,
                can_live_refresh=True,
                ai_daily_limit=10**9,
                daily_ai_left=10**9,
                live_min_interval_sec=0,
            )

        # -----------------------------
        # FREE defaults
        # -----------------------------
        ai_daily_limit = 10
        used_ai_pre = get_usage(u.id, "ai_pre", period=today, session=s)
        daily_ai_left = max(0, ai_daily_limit - used_ai_pre)

        # live: 1 trial
        can_live = not bool(u.trial_live_used)

        # live refresh: e.g. 3/day
        refresh_limit = 3
        used_refresh = get_usage(u.id, "live_refresh", period=today, session=s)
        can_live_refresh = used_refresh < refresh_limit

        # UX throttle hint (не gate, просто отображение/логика)
        live_min_interval_sec = 25

        return EffectiveEntitlements(
            tier="free",
            is_premium=False,
            can_live=can_live,
            can_live_refresh=can_live_refresh,
            ai_daily_limit=ai_daily_limit,
            daily_ai_left=daily_ai_left,
            live_min_interval_sec=live_min_interval_sec,
        )
