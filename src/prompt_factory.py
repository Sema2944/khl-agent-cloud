# src/prompt_factory.py
from __future__ import annotations

from typing import Dict, Any


SYSTEM_BASE = (
    "Ты спортивный аналитик. Пишешь по-русски, просто и понятно для широкой аудитории. "
    "Не даёшь гарантий, не используешь агрессивные призывы ('ставь', 'бери'). "
    "Пишешь короткими блоками, списками. "
    "Это аналитика, не рекомендация."
)

def build_prompt(mode: str, action: str, snapshot: Dict[str, Any]) -> Dict[str, str]:
    """
    Возвращает {system, user}
    mode: pre|live|live_pro|daily_pro
    action: overview|markets|... (что у тебя в UI)
    """
    header = (
        f"Вид спорта: {snapshot.get('sport','')}\n"
        f"Лига: {snapshot.get('league','')} • {snapshot.get('country','')}\n"
        f"Матч: {snapshot.get('home_team','')} — {snapshot.get('away_team','')}\n"
        f"Статус: {snapshot.get('status_raw','')}\n"
        f"Счёт: {snapshot.get('score','')}\n"
    ).strip()

    # Общая структура ответа (важно!)
    if mode == "pre":
        structure = (
            "Структура ответа:\n"
            "1) Что важно перед матчем (2–4 пункта)\n"
            "2) Что проверить за 30–60 минут до начала\n"
            "3) Риски (1–3 пункта)\n"
            "4) Осторожный вывод (без призывов)\n"
        )
    elif mode in ("live", "live_pro"):
        structure = (
            "Структура ответа:\n"
            "1) Что видно по игре сейчас (2–4 пункта)\n"
            "2) Темп/давление/удаления/опасные отрезки (если есть в данных)\n"
            "3) Риски и триггеры (когда лучше не лезть)\n"
            "4) Возможные сценарии на 10–20 минут вперёд\n"
        )
        if mode == "live_pro":
            structure += "5) PRO-блок: что важнее всего для линии/тоталов/форы (без навязывания)\n"
    else:  # daily_pro
        structure = (
            "Структура ответа:\n"
            "1) Топ события дня (до 3)\n"
            "2) Почему они интересны (по 2 пункта)\n"
            "3) Что проверить перед стартом\n"
            "4) Риски / когда пропустить\n"
        )

    # action влияет на “угол”
    if action == "markets":
        angle = "Сфокусируйся на рынке: линия, движение, что обычно влияет на котировки (без конкретных контор)."
    elif action == "overview":
        angle = "Сфокусируйся на общей картине матча и рисках."
    else:
        angle = "Сфокусируйся на главном и на рисках."

    user = (
        f"{header}\n\n"
        f"Данные (snapshot):\n{snapshot}\n\n"
        f"{structure}\n"
        f"{angle}\n\n"
        "Важно: если данных мало — честно скажи, чего не хватает, и дай полезный чек-лист."
    )

    return {"system": SYSTEM_BASE, "user": user}
