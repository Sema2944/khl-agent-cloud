# src/ui_text.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# -------------------------------------------------
# БАЗОВЫЕ СТРУКТУРЫ
# -------------------------------------------------

@dataclass
class MatchMeta:
    sport: str
    title: str
    league: str
    match_id: str


# 🔥 BACKWARD COMPATIBILITY
# telegram_bot/app.py уже ждёт MatchCard
# поэтому оставляем алиас
MatchCard = MatchMeta


# -------------------------------------------------
# ОБЩИЕ ТЕКСТЫ
# -------------------------------------------------

DISCLAIMER = "ℹ️ Аналитический материал. Не является рекомендацией."


def soft_throttle() -> str:
    return (
        "⏳ Обновляю данные…\n"
        "Попробуй ещё раз через пару секунд."
    )


# -------------------------------------------------
# ХАБ МАТЧА
# -------------------------------------------------

def match_hub(meta: MatchMeta) -> str:
    return (
        "🏟 Матч\n"
        f"{meta.title} — {meta.league}\n"
        f"id: {meta.match_id}\n\n"
        "Выбери раздел ниже 👇"
    )


# -------------------------------------------------
# PRE-MATCH
# -------------------------------------------------

def pre_overview(meta: MatchMeta) -> str:
    return (
        "📊 Pre-обзор\n"
        f"{meta.title} — {meta.league}\n\n"
        "Что здесь:\n"
        "• Общее состояние линии\n"
        "• Баланс спроса между исходами\n"
        "• Где рынок осторожен\n\n"
        f"{DISCLAIMER}"
    )


def pre_moneyline(meta: MatchMeta, *, home: float, draw: float, away: float) -> str:
    return (
        "🧠 1X2 / Moneyline\n"
        f"{meta.title}\n\n"
        f"П1: {home}\n"
        f"X: {draw}\n"
        f"П2: {away}\n\n"
        "Как читать рынок:\n"
        "• Сравни относительную силу сторон\n"
        "• Обрати внимание на перекос линии\n\n"
        f"{DISCLAIMER}"
    )


def pre_total(meta: MatchMeta, *, total_value: float, over: float, under: float) -> str:
    return (
        "🧠 Тотал\n"
        f"{meta.title}\n\n"
        f"Тотал: {total_value}\n"
        f"Больше: {over}\n"
        f"Меньше: {under}\n\n"
        "Интерпретация:\n"
        "• Темп и стиль игры\n"
        "• Ожидания рынка по результативности\n\n"
        f"{DISCLAIMER}"
    )


def pre_handicap(
    meta: MatchMeta,
    *,
    team: str,
    handicap_value: float,
    odds: float,
) -> str:
    side = "Хозяева" if team == "home" else "Гости"
    return (
        "🧠 Фора\n"
        f"{meta.title}\n\n"
        f"Сторона: {side}\n"
        f"Фора: {handicap_value}\n"
        f"Коэф.: {odds}\n\n"
        "Что важно:\n"
        "• Ожидание разницы в счёте\n"
        "• Давление фаворита\n\n"
        f"{DISCLAIMER}"
    )


def pre_links(meta: MatchMeta) -> str:
    return (
        "🔗 Связки рынков\n"
        f"{meta.title}\n\n"
        "Рынки редко живут отдельно:\n"
        "• Фаворит ↔ фора\n"
        "• Темп ↔ тотал\n"
        "• Осторожный рынок ↔ низкая волатильность\n\n"
        "Здесь мы смотрим на единый сценарий,\n"
        "который рынок закладывает в линию.\n\n"
        f"{DISCLAIMER}"
    )


# -------------------------------------------------
# LIVE
# -------------------------------------------------

def live_overview(meta: MatchMeta) -> str:
    return (
        "🟢 LIVE-обзор\n"
        f"{meta.title}\n\n"
        "Фокус LIVE:\n"
        "• Изменение темпа\n"
        "• Реакция рынка на события\n"
        "• Направление движения линии\n\n"
        f"{DISCLAIMER}"
    )


def live_full(meta: MatchMeta) -> str:
    return (
        "🟢 LIVE (полный разбор)\n"
        f"{meta.title}\n\n"
        "Расширенный LIVE-анализ:\n"
        "• Структура матча\n"
        "• Давление и инициатива\n"
        "• Логика изменений линии\n\n"
        "Без коэффициентов — только смысл движения.\n\n"
        f"{DISCLAIMER}"
    )


# -------------------------------------------------
# FALLBACK (если AI недоступен)
# -------------------------------------------------

def ai_fallback_pre(meta: Optional[MatchMeta]) -> str:
    title = meta.title if meta else "Матч"
    return (
        "📊 Обзор рынков\n"
        f"{title}\n\n"
        "AI временно недоступен.\n"
        "Показываю базовую справку без интерпретаций.\n\n"
        "Риски:\n"
        "• Недостаточно данных для детального анализа\n\n"
        f"{DISCLAIMER}"
    )


def ai_fallback_live(meta: Optional[MatchMeta]) -> str:
    title = meta.title if meta else "Матч"
    return (
        "🟢 LIVE-обзор\n"
        f"{title}\n\n"
        "AI временно недоступен.\n"
        "LIVE-паттерны не определены.\n\n"
        "Риски:\n"
        "• Недостаточно данных для LIVE-разбора\n\n"
        f"{DISCLAIMER}"
    )
