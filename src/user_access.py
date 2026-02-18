# src/user_access.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# Минимальный слой доступа.
# telegram_bot/app.py ожидает allowed_sports_for_user(user_id) -> List[str]
# parsing.py / ui может ожидать is_pro(user_id) — добавим совместимость.


# Load from centralized config
try:
    from .sports_config import get_default_sports
    DEFAULT_ALLOWED_SPORTS: List[str] = get_default_sports()
except Exception:
    DEFAULT_ALLOWED_SPORTS: List[str] = [
        "football",
        "ice-hockey",
        "basketball",
    ]


@dataclass
class UserAccess:
    user_id: int
    is_pro: bool = False
    is_premium: bool = False
    can_live: bool = True


# --- Простая заглушка: если у тебя есть user_store/DB — можно подцепить тут ---
def _load_user_access_from_store(user_id: int) -> Optional[UserAccess]:
    """
    Пытаемся аккуратно подключиться к существующей модели пользователя, если она есть.
    Ничего не падает, если модулей нет.
    """
    try:
        from .user_store import get_user  # type: ignore
    except Exception:
        return None

    try:
        u = get_user(int(user_id))
        return UserAccess(
            user_id=int(user_id),
            is_pro=bool(getattr(u, "is_pro", False)),
            is_premium=bool(getattr(u, "is_premium", False)),
            can_live=bool(getattr(u, "can_live", True)),
        )
    except Exception:
        return None


def allowed_sports_for_user(user_id: int) -> List[str]:
    """
    Возвращает список sport_slug, которые доступны пользователю в навигации.
    По умолчанию — всё из DEFAULT_ALLOWED_SPORTS.
    Если есть user_store и там ограничения — можно расширить логику тут.
    """
    ua = _load_user_access_from_store(int(user_id))
    # Если хочешь ограничивать free-юзеров — делай это здесь.
    # Сейчас: даём всем одинаково, чтобы бот работал.
    return list(DEFAULT_ALLOWED_SPORTS)


def is_pro(user_id: int) -> bool:
    """
    Совместимость: parsing.py/другие модули могут звать is_pro(user_id).
    """
    ua = _load_user_access_from_store(int(user_id))
    return bool(ua.is_pro) if ua else False


def can_live(user_id: int) -> bool:
    ua = _load_user_access_from_store(int(user_id))
    return bool(ua.can_live) if ua else True
