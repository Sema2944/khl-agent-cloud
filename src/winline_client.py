from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import os
import httpx


# Базовый URL Winline (можно переопределить через переменную окружения WINLINE_BASE_URL)
BASE_URL = os.getenv("WINLINE_BASE_URL", "https://cf.winlinesports.com/v3")


@dataclass
class Outcome:
    """
    Отдельный исход в маркете.
    Например:
      name = '1', price = 1.85
      name = 'ТБ 5.5', price = 1.92
    """
    id: Optional[int]
    name: str
    price: float


@dataclass
class Market:
    """
    Маркет (рынок) в событии.
    Например:
      name = '1X2' или 'Победа в матче'
      name = 'Фора'
      name = 'Тотал'
    """
    id: Optional[int]
    name: str
    key: Optional[str]
    outcomes: List[Outcome]


@dataclass
class Event:
    """
    Спортивное событие (матч).
    """
    id: int
    team1: str
    team2: str
    league: Optional[str]
    sport: Optional[str]
    start_time: Optional[datetime]
    markets: List[Market]


# ------------------- ВНУТРЕННИЕ HTTP-ВЫЗОВЫ -------------------


async def _fetch_events_raw() -> Dict[str, Any]:
    """
    Тянем список событий из Winline.
    Возвращает исходный JSON вида:
      { "data": [ {...event...}, ... ] }
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{BASE_URL}/events/list",
            params={"lang": "ru"},
        )
        resp.raise_for_status()
        return resp.json()


async def _fetch_markets_raw(event_id: int) -> Dict[str, Any]:
    """
    Тянем маркеты по конкретному событию.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{BASE_URL}/events/{event_id}/markets",
            params={"lang": "ru"},
        )
        resp.raise_for_status()
        return resp.json()


# ------------------- ПАРСИНГ Winline → наши dataclass -------------------


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """
    Пытаемся аккуратно распарсить дату/время из строки Winline.
    Если не получилось — возвращаем None, чтобы код дальше не падал.
    """
    if not value:
        return None

    # Пробуем ISO-формат: "2025-11-25T19:30:00+03:00"
    try:
        return datetime.fromisoformat(value)
    except Exception:
        pass

    # Пробуем вариант без таймзоны "2025-11-25T19:30:00"
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _parse_market(m: Dict[str, Any]) -> Optional[Market]:
    """
    Приводим один маркет к Market.
    Если нет исходов с ценами — возвращаем None.
    """
    market_id = m.get("id")
    name = m.get("name") or m.get("caption") or ""
    key = m.get("key")

    outcomes: List[Outcome] = []

    for o in m.get("outcomes", []) or []:
        price = o.get("price")
        if price is None:
            continue
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            continue

        outcomes.append(
            Outcome(
                id=o.get("id"),
                name=o.get("name") or o.get("caption") or "",
                price=price_f,
            )
        )

    if not outcomes:
        return None

    return Market(
        id=market_id,
        name=name,
        key=key,
        outcomes=outcomes,
    )


async def _build_event(e: Dict[str, Any]) -> Event:
    """
    Собираем полный Event с маркетами.
    """
    event_id = int(e["id"])
    team1 = e.get("team1", {}).get("name") or e.get("home", "") or ""
    team2 = e.get("team2", {}).get("name") or e.get("away", "") or ""
    league = e.get("leagueName")
    sport = e.get("sport")

    # В разных версиях API поле с датой может называться по-разному.
    start_raw = (
        e.get("startTime")
        or e.get("startDate")
        or e.get("date")
        or e.get("start")
    )
    start_time = _parse_datetime(start_raw)

    markets_json = await _fetch_markets_raw(event_id)
    markets: List[Market] = []

    for m in markets_json.get("markets", []) or []:
        parsed = _parse_market(m)
        if parsed:
            markets.append(parsed)

    return Event(
        id=event_id,
        team1=team1,
        team2=team2,
        league=league,
        sport=sport,
        start_time=start_time,
        markets=markets,
    )


# ------------------- ПУБЛИЧНЫЕ ФУНКЦИИ -------------------


async def get_events_by_league(league_name: str) -> List[Event]:
    """
    Забираем все события нужной лиги (например, 'KHL', 'NHL').
    Сейчас почти не фильтруем по дате — просто берём всё, что даёт Winline.
    """
    raw = await _fetch_events_raw()
    events_raw = raw.get("data", []) or []

    result: List[Event] = []

    league_norm = league_name.strip().upper()

    for e in events_raw:
        league = (e.get("leagueName") or "").strip().upper()
        if league != league_norm:
            continue

        try:
            event = await _build_event(e)
        except Exception:
            # Если что-то упало при сборке одного Event — пропускаем, но не роняем всё
            continue

        result.append(event)

    return result


async def get_khl_events_for_today() -> List[Event]:
    """
    Упрощённо: сейчас просто возвращаем все события лиги 'KHL'.
    При желании можно дальше отфильтровать по дате (start_time.date()).
    """
    return await get_events_by_league("KHL")


async def get_nhl_events_for_today() -> List[Event]:
    """
    Аналогично для 'NHL'.
    """
    return await get_events_by_league("NHL")


def format_events_short(events: List[Event]) -> str:
    """
    Утилита: сделать короткий человекочитаемый список матчей.
    Этой функцией можно пользоваться в сервисе/боте.
    """
    if not events:
        return "Нет найденных матчей."

    lines: List[str] = []
    for idx, e in enumerate(events, start=1):
        dt_str = ""
        if e.start_time:
            dt_str = e.start_time.strftime("%d.%m %H:%M")
        lines.append(
            f"{idx}) {e.team1} — {e.team2} (id: {e.id})"
            + (f", {dt_str}" if dt_str else "")
        )

    return "\n".join(lines)
