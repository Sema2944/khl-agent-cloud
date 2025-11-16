from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import httpx


BASE_URL = "https://cf.winlinesports.com/v3"


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
    league: Optional[str]
    sport: Optional[str]
    markets: List[Market]


async def _fetch_events_raw() -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{BASE_URL}/events/list", params={"lang": "ru"})
        r.raise_for_status()
        return r.json()


async def _fetch_markets_raw(event_id: int) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{BASE_URL}/events/{event_id}/markets",
            params={"lang": "ru"},
        )
        r.raise_for_status()
        return r.json()


async def get_khl_events_for_today() -> List[Event]:
    """
    Тянем все события, фильтруем leagueName == 'KHL',
    для каждого подгружаем маркеты и приводим к Event.
    СЕЙЧАС ЭТА ФУНКЦИЯ НАМ НЕ НУЖНА, НО ПУСТЬ ЛЕЖИТ ПРАВИЛЬНОЙ.
    """
    data = await _fetch_events_raw()
    events_raw = data.get("data", [])

    khl_events: List[Event] = []

    for e in events_raw:
        if e.get("leagueName") != "KHL":
            continue

        event_id = e["id"]
        team1 = e.get("team1", {}).get("name", "")
        team2 = e.get("team2", {}).get("name", "")
        league = e.get("leagueName")
        sport = e.get("sport")

        markets_resp = await _fetch_markets_raw(event_id)
        markets_list: List[Market] = []

        for m in markets_resp.get("markets", []):
            name = m.get("name", "")
            outcomes: List[Outcome] = []
            for o in m.get("outcomes", []):
                price = o.get("price")
                if price is None:
                    continue
                outcomes.append(
                    Outcome(
                        name=o.get("name", ""),
                        price=float(price),
                    )
                )

            if outcomes:
                markets_list.append(Market(name=name, outcomes=outcomes))

        khl_events.append(
            Event(
                id=event_id,
                team1=team1,
                team2=team2,
                league=league,
                sport=sport,
                markets=markets_list,
            )
        )

    return khl_events
