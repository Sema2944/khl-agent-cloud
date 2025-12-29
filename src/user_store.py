# src/user_store.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

@dataclass
class UserAccess:
    user_id: int
    is_premium: bool = False
    premium_until: Optional[datetime] = None
    trial_live_used: bool = False

    @property
    def can_live(self) -> bool:
        if self.is_premium and self.premium_until:
            return self.premium_until > datetime.now(timezone.utc)
        return False

# MVP: process-local store (на Render будет сбрасываться при рестарте!)
# Для прод — заменить на DB.
_STORE: Dict[int, UserAccess] = {}

def get_user(user_id: int) -> UserAccess:
    u = _STORE.get(int(user_id))
    if not u:
        u = UserAccess(user_id=int(user_id))
        _STORE[int(user_id)] = u
    # авто-выключение premium если истёк
    if u.is_premium and u.premium_until and u.premium_until <= datetime.now(timezone.utc):
        u.is_premium = False
        u.premium_until = None
    return u

def activate_premium(user_id: int, days: int) -> UserAccess:
    u = get_user(user_id)
    now = datetime.now(timezone.utc)
    base = u.premium_until if (u.premium_until and u.premium_until > now) else now
    u.is_premium = True
    u.premium_until = base + timedelta(days=int(days))
    _STORE[int(user_id)] = u
    return u

def mark_trial_used(user_id: int) -> UserAccess:
    u = get_user(user_id)
    u.trial_live_used = True
    _STORE[int(user_id)] = u
    return u
