# src/entitlements.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
from typing import Dict, Any, Optional, Tuple

from sqlmodel import select

from .db import get_session
from .models import User, Entitlement, UsageCounter, Subscription


@dataclass
class EffectiveEntitlements:
    tier: str
    ai_daily_limit: int
    live_min_interval_sec: int
    features: Dict[str, Any]


# дефолтные профили — потом легко менять без переписывания кода
FREE_PROFILE = EffectiveEntitlements(
    tier="free",
    ai_daily_limit=10,
    live_min_interval_sec=30,
    features={
        "live": True,
        "markets_basic": True,
        "markets_advanced": False,
    },
)

PREMIUM_PROFILE = EffectiveEntitlements(
    tier="premium",
    ai_daily_limit=200,
    live_min_interval_sec=8,
    features={
        "live": True,
        "markets_basic": True,
        "markets_advanced": True,
        "priority_llm": True,
    },
)


def _now() -> datetime:
    return datetime.utcnow()


def ensure_entitlements(user_id: int) -> None:
    """
    Гарантируем, что у пользователя есть entitlement.
    Источник истины по tier — активная подписка (если есть).
    """
    with get_session() as s:
        user = s.get(User, user_id)
        if not user:
            return

        # есть активная подписка?
        sub = s.exec(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .where(Subscription.status == "active")
            .order_by(Subscription.updated_at.desc())
        ).first()

        effective_tier = "premium" if sub else "free"

        # актуальный entitlement (по времени)
        ent = s.exec(
            select(Entitlement)
            .where(Entitlement.user_id == user_id)
            .where((Entitlement.effective_to.is_(None)) | (Entitlement.effective_to > _now()))
            .order_by(Entitlement.effective_from.desc())
        ).first()

        if ent is None:
            prof = PREMIUM_PROFILE if effective_tier == "premium" else FREE_PROFILE
            ent = Entitlement(
                user_id=user_id,
                ai_daily_limit=prof.ai_daily_limit,
                live_min_interval_sec=prof.live_min_interval_sec,
                features=prof.features,
                effective_from=_now(),
                effective_to=None,
            )
            s.add(ent)

        # поддерживаем user.tier как удобный кэш (не критично)
        if user.tier != effective_tier:
            user.tier = effective_tier

        s.commit()


def get_effective_entitlements(user_id: int) -> EffectiveEntitlements:
    ensure_entitlements(user_id)
    with get_session() as s:
        user = s.get(User, user_id)
        tier = (user.tier if user else "free") if user else "free"

        ent = s.exec(
            select(Entitlement)
            .where(Entitlement.user_id == user_id)
            .where((Entitlement.effective_to.is_(None)) | (Entitlement.effective_to > _now()))
            .order_by(Entitlement.effective_from.desc())
        ).first()

        if not ent:
            return PREMIUM_PROFILE if tier == "premium" else FREE_PROFILE

        return EffectiveEntitlements(
            tier=tier,
            ai_daily_limit=int(ent.ai_daily_limit),
            live_min_interval_sec=int(ent.live_min_interval_sec),
            features=dict(ent.features or {}),
        )


def _get_usage_row(user_id: int, day: date) -> UsageCounter:
    with get_session() as s:
        row = s.exec(
            select(UsageCounter).where(UsageCounter.user_id == user_id).where(UsageCounter.day == day)
        ).first()
        if row:
            return row
        row = UsageCounter(user_id=user_id, day=day, ai_calls=0, live_calls=0)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def check_and_consume_llm(user_id: int, *, schema: str) -> Tuple[bool, str]:
    """
    Единая точка ограничения LLM.
    schema: legacy | ui_pre | ui_live
    """
    ent = get_effective_entitlements(user_id)
    today = date.today()
    row = _get_usage_row(user_id, today)

    is_live = (schema == "ui_live")

    # лимит дневной (общий)
    if row.ai_calls >= ent.ai_daily_limit:
        return False, f"Лимит AI на сегодня исчерпан ({ent.ai_daily_limit}/день)."

    # LIVE ограничение — частота
    # (простое: ограничиваем кол-во live_calls за день и реже дергаем в коде per-user throttle тоже)
    if is_live:
        # можно дополнить “последний вызов live” таблицей событий — пока держим на llm_client throttle + этот дневной
        pass

    # consume
    with get_session() as s:
        row2 = s.exec(
            select(UsageCounter).where(UsageCounter.user_id == user_id).where(UsageCounter.day == today)
        ).first()
        if not row2:
            row2 = UsageCounter(user_id=user_id, day=today, ai_calls=0, live_calls=0)
            s.add(row2)

        row2.ai_calls += 1
        if is_live:
            row2.live_calls += 1
        row2.updated_at = _now()
        s.commit()

    return True, "ok"


def set_subscription_active(user_id: int, *, provider: str, plan_code: str, period_end: Optional[datetime]) -> None:
    """
    Это будет дергаться платежным webhook’ом.
    """
    with get_session() as s:
        sub = s.exec(
            select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.updated_at.desc())
        ).first()
        if not sub:
            sub = Subscription(user_id=user_id, provider=provider, plan_code=plan_code)
            s.add(sub)

        sub.status = "active"
        sub.current_period_end = period_end
        sub.updated_at = _now()
        s.commit()

    ensure_entitlements(user_id)
