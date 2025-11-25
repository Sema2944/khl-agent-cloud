# src/winline_client.py
from __future__ import annotations
import os
import httpx
from dataclasses import dataclass
from typing import List, Optional, Dict, Any

# --- ВАЖНО ---
# Если указан прокси — используем его.
# Иначе — прямой Winline (локальная разработка).
BASE_URL = os.getenv("WINLINE_PROXY_BASE_URL", "https://cf.winlinesports.com/v3")


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
    start_time: Optional[str]
    league: str
    markets: List[Market]


async def fetch_events() -> List[Dict[str, Any]]:
    """
    /events/list через Winline или прокси.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BASE_URL}/events/list", params={"lang": "ru"})
        r.raise_for_status()
        data = r.json()
        return data.get("data", [])


async def fetch_markets(event_id: int) -> Dict[str, Any]:
    """
    /events/<id>/markets через Winline или прокси.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{BASE_URL}/events/{event_id}/markets",
            params={"lang": "ru"},
        )
        r.raise_for_status()
        return r.json()


async def get_khl_events_today() -> List[Event]:
    """
    Забираем матчи КХЛ + всю линию по ним.
    """
    events_raw = await fetch_events()
    result: List[Event] = []

    for ev in events_raw:
        if ev.get("leagueName") != "KHL":
            continue

        event_id = ev["id"]
        team1 = ev.get("team1", {}).get("name", "")
        team2 = ev.get("team2", {}).get("name", "")
        start_time = ev.get("date")

        # Загружаем маркеты
        markets_json = await fetch_markets(event_id)

        markets: List[Market] = []
        for m in markets_json.get("markets", []):
            name = m.get("name", "")
            outcomes: List[Outcome] = []

            for o in m.get("outcomes", []):
                price = o.get("price")
                if price is None:
                    continue

                outcomes.append(
                    Outcome(
                        name=o.get("name", ""),
                        price=float(price)
                    )
                )

            if outcomes:
                markets.append(Market(name=name, outcomes=outcomes))

        result.append(
            Event(
                id=event_id,
                team1=team1,
                team2=team2,
                start_time=start_time,
                league="KHL",
                markets=markets,
            )
        )

    return result


async def format_khl_today_text() -> str:
    """
    Форматируем текст для вывода матчи КХЛ сегодня.
    """
    events = await get_khl_events_today()
    if not events:
        return "Сегодня нет матчей КХЛ (или Winline не даёт данные)."

    lines = ["🏒 Матчи КХЛ на сегодня:\n"]

    for ev in events:
        line = f"{ev.id}: {ev.team1} — {ev.team2}"

        # Ищем маркет 1X2
        m1x2 = next(
            (m for m in ev.markets if m.name.upper() in ("1X2", "3WAY", "ИСХОД")),
            None,
        )

        if m1x2:
            odds = {o.name: o.price for o in m1x2.outcomes}
            o1 = odds.get("1")
            ox = odds.get("X")
            o2 = odds.get("2")

            if o1 and ox and o2:
                line += f" | 1X2: {o1} / {ox} / {o2}"

        lines.append(line)

    lines.append("\nЧтобы получить анализ матча: «анализ матча <id>»")
    return "\n".join(lines)
