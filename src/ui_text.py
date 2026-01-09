# src/ui_text.py
"""
ui_text.py — стабильный слой текстов UI.
НЕ содержит логики, НЕ зависит от API, НЕ импортирует parsing.
Можно безопасно расширять.
"""

from typing import Optional


# -----------------------------
# БАЗОВЫЕ ЭКРАНЫ
# -----------------------------

def text_internal_error() -> str:
    return (
        "⚠️ Внутренняя ошибка.\n\n"
        "Сейчас чиню модуль аналитики.\n"
        "Пожалуйста, попробуй ещё раз через минуту."
    )


def text_choose_sport() -> str:
    return "🏟 Выбери спорт:"


def text_ai_help() -> str:
    return (
        "Как пользоваться:\n"
        "1) 🏟 Матчи сегодня\n"
        "2) спорт → матч\n"
        "3) в матче нажми: Pre / LIVE / рынки\n\n"
        "Диагностика:\n"
        "llm ping, env, version, last_error"
    )


# -----------------------------
# МАТЧ
# -----------------------------

def text_match_header(
    title: str,
    league: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    lines = [f"🏟 Матч\n{title}"]
    if league:
        lines.append(f"Лига: {league}")
    if status:
        lines.append(f"Статус: {status}")
    lines.append("\nВыбери действие кнопками ниже 👇")
    return "\n".join(lines)


# -----------------------------
# PRE / LIVE
# -----------------------------

def text_pre_overview_stub() -> str:
    return (
        "📊 Обзор рынков\n\n"
        "AI временно недоступен — показываю базовую справку.\n\n"
        "Риски:\n"
        "• Недостаточно данных для детального разбора.\n\n"
        "ℹ️ Аналитический материал. Не является рекомендацией."
    )


def text_live_stub() -> str:
    return (
        "🟢 LIVE-обзор\n\n"
        "AI временно недоступен — показываю базовую справку.\n\n"
        "Риски:\n"
        "• Недостаточно данных для LIVE-разбора.\n\n"
        "ℹ️ Аналитический материал. Не является рекомендацией."
    )


# -----------------------------
# PREMIUM
# -----------------------------

def text_premium(
    is_premium: bool,
    ai_left: int,
    live_left: int,
    min_pause_sec: int,
) -> str:
    status = "PREMIUM ✅" if is_premium else "FREE"
    return (
        "⭐ Premium\n\n"
        "Premium — это доступ к расширенной аналитике матчей.\n\n"
        f"Текущий доступ: {status}\n"
        f"Лимит AI/день: {ai_left}\n"
        f"LIVE refresh/день: {live_left}\n"
        f"Минимальная пауза LIVE: {min_pause_sec} сек\n\n"
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
