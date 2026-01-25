from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
import time

Plan = Literal["free", "premium"]

DEFAULT_SPORTS = [
    "ice-hockey",
    "football",
    "basketball",
    "tennis",
    "table-tennis",
    "esports",
]


@dataclass
class UserAccess:
    user_id: int
    plan: Plan
    trial_live_used: bool
    premium_until_ts: int | None  # unix ts

    @property
    def is_premium(self) -> bool:
        if self.plan != "premium":
            return False
        if self.premium_until_ts is None:
            return True
        return self.premium_until_ts > int(time.time())

    @property
    def can_live(self) -> bool:
        return self.is_premium or not self.trial_live_used


def allowed_sports_for_user() -> list[str]:
    """
    Пока без персональной логики — возвращаем набор доступных видов спорта.
    В будущем можно подключить user_id и тарифы.
    """
    return list(DEFAULT_SPORTS)
