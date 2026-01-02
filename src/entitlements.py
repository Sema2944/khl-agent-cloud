# src/entitlements.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from sqlmodel import Session, select

from .models import User, Entitlement, UsageCounter


@dataclass
class EffectiveEntitlements:
    is_premium: bool
    can_live: bool
    can_live_refresh: bool
    daily_ai_left: int


def get_effective_entitlements(session: Session, tg_user_id: int) -> EffectiveEntitlements:
    """
    Политика:
    - Premium: полный доступ.
    - Free: live = только trial 1 раз (или можно дать лимиты),
            live_refresh ограничиваем,
            pre-ai ограничиваем по дневному лимиту.
    """
    user = session.exec(select(User).where(User.tg_user_id == tg_user_id)).first()
    if user is None:
        user = User(tg_user_id=tg_user_id)
        session.add(user)
        session.commit()
        session.refresh(user)

    if user.is_premium:
        return EffectiveEntitlements(
            is_premium=True,
            can_live=True,
            can_live_refresh=True,
            daily_ai_left=10**9,
        )

    # FREE — пример лимитов
    today = date.today()

    # дневной лимит на pre-ai (пример: 10)
    daily_limit = 10
    c = session.exec(
        select(UsageCounter).where(
            UsageCounter.user_id == user.id,
            UsageCounter.key == "ai_pre",
            UsageCounter.period_start == today,
        )
    ).first()
    used = c.count if c else 0
    daily_ai_left = max(0, daily_limit - used)

    # LIVE доступ: 1 trial
    can_live = (not user.trial_live_used)

    # refresh: free можно запретить вообще или дать, например, 3 в день
    refresh_limit = 3
    rc = session.exec(
        select(UsageCounter).where(
            UsageCounter.user_id == user.id,
            UsageCounter.key == "live_refresh",
            UsageCounter.period_start == today,
        )
    ).first()
    rused = rc.count if rc else 0
    can_live_refresh = rused < refresh_limit

    return EffectiveEntitlements(
        is_premium=False,
        can_live=can_live,
        can_live_refresh=can_live_refresh,
        daily_ai_left=daily_ai_left,
    )
