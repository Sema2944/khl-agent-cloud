# src/hockey_logic.py

"""
Хоккейная «турнирная логика» без живого API.

Задача модуля:
- На вход: названия двух команд и (опционально) лига.
- На выход: текстовые заметки про контекст матча:
  * борьба за верх / плей-офф / выживание;
  * сценарий «топ против аутсайдера»;
  * возможная мотивация / расслабленность фаворита.
"""

from __future__ import annotations

from typing import Literal


LeagueName = Literal["KHL", "NHL", "OTHER"]


# Грубое деление команд КХЛ (эвристика для текстовых подсказок)
KHL_TOP_TEAMS = {
    "СКА",
    "ЦСКА",
    "АК БАРС",
    "АВАНГАРД",
    "ДИНАМО МСК",
    "ТОРПЕДО",
    "ЛОКОМОТИВ",
}

KHL_MID_TEAMS = {
    "САЛАВАТ ЮЛАЕВ",
    "СПАРТАК",
    "СЕВЕРСТАЛЬ",
    "АВТОМОБИЛИСТ",
    "МЕТАЛЛУРГ",
    "ТРАКТОР",
    "ЙОКЕРИТ",
}

KHL_BOTTOM_TEAMS = {
    "СОЧИ",
    "АМУР",
    "КУНЬЛУНЬ",
    "НЕФТЕХИМИК",
}


def _normalize(name: str) -> str:
    return (name or "").strip().upper()


def _get_khl_tier(name: str) -> str:
    """Возвращает: 'top', 'mid', 'bottom', 'unknown'"""
    n = _normalize(name)

    if n in KHL_TOP_TEAMS:
        return "top"
    if n in KHL_BOTTOM_TEAMS:
        return "bottom"
    if n in KHL_MID_TEAMS:
        return "mid"

    return "unknown"


def build_match_context_notes(
    team1: str,
    team2: str,
    league: LeagueName = "KHL",
) -> str:
    """
    Создаёт текстовый блок «турнирный контекст» для разбора матча.
    """

    t1 = (team1 or "").strip()
    t2 = (team2 or "").strip()

    if not t1 or not t2:
        return ""

    if league != "KHL":
        return (
            "Пока турнирный контекст полностью настроен только для КХЛ. "
            "Здесь стоит смотреть на таблицу, свежесть и календарь."
        )

    tier1 = _get_khl_tier(t1)
    tier2 = _get_khl_tier(t2)

    # Теги
    def _label(tier: str) -> str:
        return {
            "top": "верх таблицы / претендент",
            "mid": "середина таблицы",
            "bottom": "нижняя часть таблицы",
            "unknown": "неопределённый статус (нужна таблица)",
        }[tier]

    lines: list[str] = []

    # Общая характеристика
    lines.append(f"{t1}: {_label(tier1)}.")
    lines.append(f"{t2}: {_label(tier2)}.")
    lines.append("")

    # ——— Сценарии матчей ———

    # Топ vs дно
    if {tier1, tier2} == {"top", "bottom"}:
        fav = t1 if tier1 == "top" else t2
        dog = t2 if fav == t1 else t1

        lines.append(f"{fav} — условный гранд, {dog} — представитель нижней части таблицы.")
        lines.append(
            "В таких матчах фаворит не всегда играет на 100%: возможна ротация, экономия сил и игра «по счёту»."
        )
        lines.append(
            "Аутсайдер способен держаться, если зацепится за первый период или вратарь поймает игру."
        )
        lines.append(
            "Если у фаворита через 1–2 дня матч с прямым конкурентом, мотивация здесь может быть ниже."
        )

    # Топ vs топ
    elif tier1 == "top" and tier2 == "top":
        lines.append(
            "Матч двух команд верхнего уровня: высокая мотивация, ценность очков максимальна."
        )
        lines.append(
            "Старт обычно осторожный, а после первого гола игра может резко раскрываться."
        )

    # Серёдка vs серёдка
    elif tier1 == "mid" and tier2 == "mid":
        lines.append("Встреча двух середняков: огромная роль у текущей формы и состава.")
        lines.append("Часто рынок ошибается в оценке таких матчей — здесь можно искать value.")

    # Топ vs середняк
    elif {"top", "mid"} == {tier1, tier2}:
        lines.append(
            "Фаворит есть, но середняк способен долго удерживать матч, особенно дома или при хорошем вратаре."
        )

    # Любой с участием дна
    elif tier1 == "bottom" or tier2 == "bottom":
        lines.append(
            "Одна из команд идёт внизу таблицы. Многое решает мотивация: борьба за плей-офф,"
            " кризис, смена тренера или игра 'без давления'."
        )

    lines.append("")
    # Главная мысль, которую ты объяснял
    lines.append(
        "Топовые команды часто распределяют усилия: важнее не отпускать прямых конкурентов, "
        "чем каждый раз громить аутсайдера."
    )
    lines.append(
        "Поэтому часть матчей против слабых соперников они играют прагматично, "
        "а полный фокус включают против команд из своего круга."
    )

   # ------------------------------------------
# KHL TODAY (WINLINE)
# ------------------------------------------

from .winline_client import get_khl_events_today


async def khl_today_text_from_winline() -> str:
    """
    Строит текст для команды 'КХЛ сегодня' на основе реальной линии Winline.
    """
    try:
        events = await get_khl_events_today()
    except Exception as e:
        logging.exception("Ошибка при запросе линии Winline: %s", e)
        return (
            "Не смог получить реальные матчи КХЛ (ошибка парсера или API Winline).\n\n"
            "🏒 Матчи КХЛ на сегодня (демо-режим):\n\n"
            "1) СКА — ЦСКА (id: 123456)\n"
            "   Линия 1X2 (пример):\n"
            "   • 1 — 1.85\n"
            "   • X — 3.90\n"
            "   • 2 — 2.10\n\n"
            "Позже сюда добавим реальные матчи, парсинг линии и аналитику по форме команд."
        )

    if not events:
        return (
            "Похоже, на сегодня в Winline нет матчей КХЛ, либо парсер не нашёл их в линии.\n\n"
            "Попробуй позже — когда линия обновится."
        )

    lines: list[str] = []
    lines.append("🏒 Матчи КХЛ на сегодня (по линии Winline):\n")

    for idx, e in enumerate(events, start=1):
        lines.append(f"{idx}) {e.team1} — {e.team2} (id: {e.id})")

        main_market = None
        for m in e.markets:
            name_upper = (m.name or "").upper()
            if "1X2" in name_upper or "ИСХОД" in name_upper or "ПОБЕДА В МАТЧЕ" in name_upper:
                main_market = m
                break

        if main_market:
            lines.append(f"   Линия {main_market.name}:")
            for o in main_market.outcomes:
                lines.append(f"   • {o.name} — {o.price}")
        else:
            lines.append("   Линия 1X2 недоступна или не распознана.")

        lines.append("")

    lines.append(
        "Выбери матч и запомни id (например, из скобок),\n"
        "а затем попроси: 'анализ матча <id>' — я разберу его подробнее."
    )

    return "\n".join(lines)
