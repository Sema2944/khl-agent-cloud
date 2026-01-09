# src/ui_text.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ------------------------------------------------------------
# Совместимость с тем, что уже импортирует telegram_bot/app.py
# (в логах у тебя были: MatchCard, LiveState, text_match,
#  text_live_full, text_match, text_live_full и т.п.)
# ------------------------------------------------------------

@dataclass
class MatchCard:
    id: str
    title: str
    league: str = ""
    status: str = ""
    start_time: str = ""
    sport_slug: str = ""


@dataclass
class LiveState:
    ts: int = 0
    # можно хранить произвольные поля, чтобы app.py не падал, если ожидает что-то ещё
    data: Optional[Dict[str, Any]] = None


def text_internal_error() -> str:
    return (
        "⚠️ Внутренняя ошибка.\n\n"
        "Сейчас чиню модуль аналитики.\n"
        "Пожалуйста, попробуй ещё раз.\n\n"
        "Если ты админ: проверь логи Render."
    )


def text_how_to() -> str:
    return (
        "Как пользоваться:\n"
        "1) 🏟 Матчи сегодня\n"
        "2) спорт → матч\n"
        "3) в матче нажми: Pre / LIVE / рынки\n\n"
        "Диагностика: llm ping, env, version, last_error"
    )


def text_premium(
    access: str = "FREE",
    ai_limit_day: int = 10,
    ai_left: int = 10,
    live_refresh_day: int = 3,
    live_left: int = 3,
    live_min_pause_s: int = 8,
) -> str:
    return (
        "⭐ Premium\n\n"
        "Premium — это доступ к расширенной аналитике матчей.\n\n"
        f"Текущий доступ: {access}\n"
        f"Лимит AI/день: {ai_limit_day} (осталось {ai_left})\n"
        f"LIVE refresh/день: {live_refresh_day} (осталось {live_left})\n"
        f"Минимальная пауза LIVE: {live_min_pause_s} сек\n\n"
        "Что внутри:\n"
        "🟢 LIVE-анализ\n"
        "• темп, структура и реакции на события\n"
        "• обновления без лимитов\n\n"
        "🧠 Глубина рынков\n"
        "• 1X2 / Тотал / Фора — логика линии\n\n"
        "🔗 Связки рынков\n"
        "• один сценарий — разные рынки\n\n"
        "ℹ️ Аналитический материал. Не является рекомендацией."
    )


def text_match(card: MatchCard) -> str:
    lines: List[str] = []
    lines.append("🏟 Матч")
    lines.append(card.title)
    if card.league:
        lines.append(f"Лига: {card.league}")
    if card.sport_slug:
        lines.append(f"Sport: {card.sport_slug}")
    if card.status:
        lines.append(f"Статус: {card.status}")
    if card.start_time:
        lines.append(f"Старт: {card.start_time}")
    lines.append(f"id: {card.id}")
    lines.append("")
    lines.append("Выбери действие кнопками ниже 👇")
    lines.append("")
    lines.append("ℹ️ Аналитический материал. Не является рекомендацией.")
    return "\n".join(lines)


def text_live_full(title: str, bullets: Optional[List[str]] = None) -> str:
    lines = [f"🟢 LIVE — {title}"]
    if bullets:
        lines.append("")
        for b in bullets[:8]:
            lines.append(f"• {b}")
    lines.append("")
    lines.append("ℹ️ Аналитический материал. Не является рекомендацией.")
    return "\n".join(lines)
