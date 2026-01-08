# src/ui_text.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# ---------------------------------------------------------------------
# Совместимость с импортами из parsing.py
# ---------------------------------------------------------------------
@dataclass
class LiveState:
    """Минимальный контейнер состояния LIVE (если где-то храните снапшоты)."""
    ts: int
    payload: dict[str, Any]


@dataclass
class MatchCard:
    """Минимальная карточка матча (на будущее / совместимость)."""
    match_id: str
    title: str
    league: str
    sport: Optional[str] = None


# ---------------------------------------------------------------------
# Базовые дисклеймеры / системные тексты
# ---------------------------------------------------------------------
def disclaimer() -> str:
    return "ℹ️ Аналитический материал. Не является рекомендацией."


def text_menu_hint() -> str:
    return "Выбирай действие кнопками ниже 👇"


def text_ai_help() -> str:
    return (
        "🧠 AI Аналитика\n\n"
        "Как пользоваться:\n"
        "1) 🏟 Матчи сегодня\n"
        "2) спорт → матч\n"
        "3) в матче нажми: 📊 Обзор / 🧠 1X2 / 🧠 Тотал / 🧠 Фора / 🔗 Связки\n"
        "4) LIVE: 🟢 LIVE или 🔄 Обновить\n\n"
        "Диагностика: llm ping, env, version, last_error\n\n"
        f"{disclaimer()}"
    )


def text_premium_screen(
    *,
    tier: str = "FREE",
    ai_daily_limit: int = 0,
    daily_ai_left: int = 0,
    live_refresh_daily_limit: int = 0,
    live_refresh_left: int = 0,
    live_min_interval_sec: int = 0,
) -> str:
    return (
        "⭐ Premium\n\n"
        "Premium — это доступ к расширенной аналитике матчей.\n\n"
        f"Текущий доступ: {tier}\n"
        f"Лимит AI/день: {ai_daily_limit} (осталось {daily_ai_left})\n"
        f"LIVE refresh/день: {live_refresh_daily_limit} (осталось {live_refresh_left})\n"
        f"Минимальная пауза LIVE: {int(live_min_interval_sec)} сек\n\n"
        "Что внутри:\n"
        "🟢 LIVE-анализ\n"
        "• темп, структура и реакции на события\n"
        "• обновления без лимитов\n\n"
        "🧠 Глубина рынков\n"
        "• 1X2 / Тотал / Фора — логика линии\n\n"
        "🔗 Связки рынков\n"
        "• один сценарий — разные рынки\n\n"
        f"{disclaimer()}"
    )


def text_live_paywall() -> str:
    # Мягкий paywall без “плати/доступ закрыт” в лоб
    return (
        "🟢 LIVE — расширенный режим\n\n"
        "В LIVE ты получаешь:\n"
        "• динамику матча (что меняется по ходу)\n"
        "• логику движения рынков\n"
        "• сравнение с прошлым обновлением\n\n"
        "Открывается в Premium.\n\n"
        f"{disclaimer()}"
    )


def text_too_frequent() -> str:
    return (
        "⏱️ Немного притормозим\n"
        "Сейчас слишком много запросов подряд. Попробуй ещё раз через пару секунд.\n\n"
        f"{disclaimer()}"
    )


def text_ai_unavailable(title: str = "📊 Обзор") -> str:
    return (
        f"{title}\n\n"
        "Сейчас AI недоступен — покажу базовую справку.\n\n"
        "Риски\n"
        "• недостаточно данных для детального разбора\n\n"
        f"{disclaimer()}"
    )


# ---------------------------------------------------------------------
# Эталонные экраны матча / рынков (то, чего не хватало: text_match)
# ---------------------------------------------------------------------
def text_match(title: str, league: str, match_id: str) -> str:
    """
    Экран "Матч" (хаб) — используется parsing.py / telegram hub.
    """
    return (
        "🏟 Матч\n"
        f"{title} — {league}\n"
        f"id: {match_id}\n\n"
        "Выбери раздел ниже 👇\n\n"
        f"{disclaimer()}"
    )


def text_pre_overview(title: str, league: str) -> str:
    return (
        "📊 Pre-match — Обзор\n"
        f"{title} ({league})\n\n"
        "Что здесь будет:\n"
        "• краткая картина линии\n"
        "• факторы, которые двигают рынок\n"
        "• риски интерпретации\n\n"
        f"{disclaimer()}"
    )


def text_pre_1x2(title: str, league: str) -> str:
    return (
        "🧠 1X2 — логика линии\n"
        f"{title} ({league})\n\n"
        "Смотрим:\n"
        "• как рынок оценивает базовый сценарий\n"
        "• где заложена «ничья/камбэк/контроль»\n"
        "• что может сдвинуть баланс\n\n"
        f"{disclaimer()}"
    )


def text_pre_total(title: str, league: str) -> str:
    return (
        "🧠 Тотал — логика порога\n"
        f"{title} ({league})\n\n"
        "Смотрим:\n"
        "• ожидание темпа/результативности\n"
        "• почему двигают порог, а не только кэф\n"
        "• какие сигналы в связке с 1X2/форой\n\n"
        f"{disclaimer()}"
    )


def text_pre_handicap(title: str, league: str) -> str:
    return (
        "🧠 Фора — поиск перекоса\n"
        f"{title} ({league})\n\n"
        "Смотрим:\n"
        "• где рынок «закрепляет» преимущество\n"
        "• что говорит фора про сценарий матча\n"
        "• когда фора расходится с 1X2/тоталом\n\n"
        f"{disclaimer()}"
    )


def text_pre_links(title: str, league: str) -> str:
    return (
        "🔗 Связки рынков\n"
        f"{title} ({league})\n\n"
        "Ищем согласованность:\n"
        "• 1X2 ↔ тотал\n"
        "• фора ↔ тотал\n"
        "• что «главное» в движении линии\n\n"
        f"{disclaimer()}"
    )


def text_live_overview(title: str, league: str) -> str:
    return (
        "🟢 LIVE — обзор\n"
        f"{title} ({league})\n\n"
        "В LIVE показываем:\n"
        "• как меняется структура игры\n"
        "• какие рынки реагируют первыми\n"
        "• куда «смотрит» линия (без цифр)\n\n"
        f"{disclaimer()}"
    )


# ---------------------------------------------------------------------
# На всякий случай: старые/переименованные импорты
# (чтобы не ловить ImportError после рефакторинга parsing.py)
# ---------------------------------------------------------------------
help_ai_text = text_ai_help
premium_screen_text = text_premium_screen
live_paywall_text = text_live_paywall
too_frequent_text = text_too_frequent
fallback_ai_unavailable_text = text_ai_unavailable
