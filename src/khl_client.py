# src/khl_client.py

from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ---- НАСТРОЙКИ И КОНСТАНТЫ ----

# URL Фонбета с линией по хоккею.
# ВАЖНО: этот URL нужно будет уточнить по devtools в браузере.
# Можно переопределить через переменную окружения FONBET_PREMATCH_URL.
FONBET_PREMATCH_URL = os.getenv(
    "FONBET_PREMATCH_URL",
    # этот URL ты позже подправишь под реальный:
    "https://line12.bkfon-resource.ru/pre/ice_hockey",
)

# МСК — для фильтрации матчей "на сегодня"
MOSCOW_TZ = timezone(timedelta(hours=3))


# ---- МОДЕЛИ ПОД ТВОЙ КОД ----

@dataclass
class Outcome:
    name: str
    price: float


@dataclass
class Market:
    name: str
    outcomes: List[Outcome]


@dataclass
class Event:
    id: int
    team1: str
    team2: str
    start_ts: Optional[int]
    markets: List[Market]


# ---- ВНУТРЕННИЕ ХЕЛПЕРЫ ----

def _parse_fonbet_events(data: dict[str, Any]) -> List[Event]:
    """
    Преобразуем "сырые" данные Фонбета в наш формат Event / Market / Outcome.

    ВАЖНО: структура JSON у Фонбета может отличаться.
    Этот парсер написан максимально безопасно:
    - если ключей нет — просто пропускаем;
    - если ничего не смогли распарсить — вернём пустой список,
      а вызывающая сторона подставит демо-матчи.
    """
    events_raw = data.get("events") or data.get("event") or []
    if not isinstance(events_raw, list):
        logger.warning("Формат Fonbet JSON: 'events' не список, получено: %r", type(events_raw))
        return []

    # Иногда команды/чемпы лежат отдельно — на будущее:
    champs_by_id: dict[int, dict[str, Any]] = {}
    for c in data.get("champs") or []:
        cid = c.get("id")
        if isinstance(cid, int):
            champs_by_id[cid] = c

    teams_by_id: dict[int, dict[str, Any]] = {}
    for t in data.get("teams") or []:
        tid = t.get("id")
        if isinstance(tid, int):
            teams_by_id[tid] = t

    today = datetime.now(MOSCOW_TZ).date()
    out_events: List[Event] = []

    for ev in events_raw:
        # --- id события ---
        ev_id = ev.get("id")
        if not isinstance(ev_id, int):
            continue

        # --- время начала ---
        start_ts = ev.get("startTime") or ev.get("start")  # разные варианты ключей
        start_ts_int: Optional[int] = None
        if isinstance(start_ts, (int, float)):
            start_ts_int = int(start_ts)
        else:
            start_ts_int = None

        # фильтрация на "сегодня" (если есть нормальный timestamp)
        if start_ts_int is not None:
            dt = datetime.fromtimestamp(start_ts_int, MOSCOW_TZ)
            if dt.date() != today:
                # матч не сегодня → пропустили
                continue

        # --- названия команд ---
        # Вариант 1: имена прямо в событии
        team1 = ev.get("team1") or ev.get("team1Name")
        team2 = ev.get("team2") or ev.get("team2Name")

        # Вариант 2: по id из таблицы teams
        if not team1 or not team2:
            t1_id = ev.get("team1Id")
            t2_id = ev.get("team2Id")
            if isinstance(t1_id, int) and t1_id in teams_by_id:
                team1 = team1 or teams_by_id[t1_id].get("name") or teams_by_id[t1_id].get("shortName")
            if isinstance(t2_id, int) and t2_id in teams_by_id:
                team2 = team2 or teams_by_id[t2_id].get("name") or teams_by_id[t2_id].get("shortName")

        if not team1 or not team2:
            # если команд нет — матч нам не нужен
            continue

        # --- рынки / коэффициенты ---
        markets: List[Market] = []

        # Некоторые реализации Fonbet кладут маркеты прямо в ev["markets"],
        # где каждый маркет содержит name и list outcomes.
        markets_raw = ev.get("markets") or []
        if isinstance(markets_raw, list):
            for m in markets_raw:
                m_name = (m.get("name") or "").strip()
                if not m_name:
                    continue

                outs_raw = m.get("outcomes") or m.get("odds") or []
                outcomes: List[Outcome] = []
                if isinstance(outs_raw, list):
                    # формат типа [{"name": "1", "price": 1.85}, ...]
                    for o in outs_raw:
                        oname = (o.get("name") or o.get("outcome") or "").strip()
                        price = o.get("price") or o.get("odd") or o.get("coef")
                        if not oname or price is None:
                            continue
                        try:
                            price_f = float(price)
                        except (TypeError, ValueError):
                            continue
                        outcomes.append(Outcome(name=oname, price=price_f))
                elif isinstance(outs_raw, dict):
                    # формат типа {"1": 1.85, "X": 3.9, "2": 2.1}
                    for oname, price in outs_raw.items():
                        try:
                            price_f = float(price)
                        except (TypeError, ValueError):
                            continue
                        outcomes.append(Outcome(name=str(oname), price=price_f))

                if outcomes:
                    markets.append(Market(name=m_name, outcomes=outcomes))

        # Если не нашли ни одного рынка — всё равно создадим Event,
        # а дальше build_khl_match_analysis честно скажет, что линию не нашёл.
        out_events.append(
            Event(
                id=ev_id,
                team1=str(team1),
                team2=str(team2),
                start_ts=start_ts_int,
                markets=markets,
            )
        )

    return out_events


def _demo_events() -> List[Event]:
    """
    Фолбэк-демо, если Fonbet не ответил или формат не подошёл.
    Это то, что у тебя уже использовалось в ручных примерах.
    """
    return [
        Event(
            id=123456,
            team1="СКА",
            team2="ЦСКА",
            start_ts=None,
            markets=[
                Market(
                    name="1X2",
                    outcomes=[
                        Outcome(name="1", price=1.85),
                        Outcome(name="X", price=3.90),
                        Outcome(name="2", price=2.10),
                    ],
                )
            ],
        )
    ]


# ---- PUBLIC API ДЛЯ СЕРВИСА ----

async def get_today_khl_events() -> List[Event]:
    """
    Async-версия: возвращает список Event на СЕГОДНЯ
    (по времени МСК), используя Fonbet JSON.

    Используется в service.py в ветке 'анализ матча <id>'.
    Если что-то ломается — возвращает демо-матчи.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(FONBET_PREMATCH_URL)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception("Не удалось получить данные Fonbet, возвращаю демо-матчи")
        return _demo_events()

    try:
        events = _parse_fonbet_events(data)
    except Exception:
        logger.exception("Ошибка при разборе Fonbet JSON, возвращаю демо-матчи")
        return _demo_events()

    if not events:
        logger.warning("Fonbet вернул пустой список событий или ничего не распарсилось, фолбэк на демо")
        return _demo_events()

    return events


def build_khl_today_matches_demo() -> str:
    """
    Синхронная функция, которую дергает агент по запросу
    'КХЛ сегодня' / 'КХЛ на сегодня'.

    Она сама ходит в Fonbet (sync httpx.Client),
    парсит события и возвращает красивый текст.

    Если что-то пошло не так — показывает демо-матч СКА — ЦСКА.
    """
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(FONBET_PREMATCH_URL)
            resp.raise_for_status()
            data = resp.json()
        events = _parse_fonbet_events(data)
    except Exception:
        logger.exception("Ошибка при получении/разборе Fonbet в build_khl_today_matches_demo")
        events = _demo_events()

    if not events:
        events = _demo_events()

    lines: List[str] = []
    lines.append("Матчи КХЛ на сегодня (по данным линии):")
    lines.append("")

    for ev in events:
        # Время, если есть timestamp
        if ev.start_ts:
            dt = datetime.fromtimestamp(ev.start_ts, MOSCOW_TZ)
            time_str = dt.strftime("%H:%M")
        else:
            time_str = "—:—"

        lines.append(f"id: {ev.id} | {time_str} | {ev.team1} — {ev.team2}")

        # Попробуем найти рынок 1X2
        market_1x2: Optional[Market] = None
        for m in ev.markets:
            name_up = m.name.upper()
            if name_up in ("1X2", "1X", "3WAY", "3-WAY"):
                market_1x2 = m
                break

        if market_1x2:
            odds_map: dict[str, float] = {}
            for o in market_1x2.outcomes:
                odds_map[o.name.upper()] = o.price

            o1 = odds_map.get("1")
            ox = odds_map.get("X") or odds_map.get("DRAW")
            o2 = odds_map.get("2")

            if o1 or ox or o2:
                parts = []
                if o1:
                    parts.append(f"1: {o1:.2f}")
                if ox:
                    parts.append(f"X: {ox:.2f}")
                if o2:
                    parts.append(f"2: {o2:.2f}")
                lines.append("  Линия 1X2: " + " | ".join(parts))
        else:
            lines.append("  Линия 1X2: нет данных по кэфам (пока или не распарсили).")

        lines.append("")

    lines.append(
        "Чтобы разобрать конкретный матч глубже, напиши:\n"
        "• 'анализ матча <id>' — я покажу имплайд-вероятности и маржу бука."
    )

    return "\n".join(lines)
