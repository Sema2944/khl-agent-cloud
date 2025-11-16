from __future__ import annotations

import os
import datetime as dt
from typing import List, Dict, Any

import httpx
from .db import OddsEvent, upsert_events

BOOKMAKER_URL = os.getenv("BOOKMAKER_URL", "").strip()

async def fetch_raw() -> List[Dict[str, Any]]:
    if not BOOKMAKER_URL:
        # демо-данные (если не настроен URL)
        return [
            {
                "id": "demo-1",
                "sport": "Soccer",
                "league": "Premier League",
                "team1": "Arsenal",
                "team2": "Chelsea",
                "starts_at": (dt.datetime.utcnow() + dt.timedelta(hours=3)).isoformat(),
                "odds1": 1.95,
                "oddsX": 3.6,
                "odds2": 3.9,
            },
            {
                "id": "demo-2",
                "sport": "Hockey",
                "league": "KHL",
                "team1": "SKA",
                "team2": "CSKA",
                "starts_at": (dt.datetime.utcnow() + dt.timedelta(hours=6)).isoformat(),
                "odds1": 2.1,
                "oddsX": 3.2,
                "odds2": 3.2,
            },
        ]
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(BOOKMAKER_URL)
        r.raise_for_status()
        data = r.json()
        # ожидаем либо список, либо объект с ключом "events"
        if isinstance(data, dict) and "events" in data:
            return data["events"]  # type: ignore
        if isinstance(data, list):
            return data  # type: ignore
        return []

def _to_dt(value: str | None):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

def parse_payload(rows: List[Dict[str, Any]]) -> List[OddsEvent]:
    events: List[OddsEvent] = []
    for r in rows:
        ev = OddsEvent(
            ext_id=str(r.get("id")),
            sport=(r.get("sport") or None),
            league=(r.get("league") or None),
            team1=(r.get("team1") or r.get("home") or None),
            team2=(r.get("team2") or r.get("away") or None),
            starts_at=_to_dt(r.get("starts_at")),
            odds1=_safe_float(r.get("odds1")),
            oddsX=_safe_float(r.get("oddsX") or r.get("draw")),
            odds2=_safe_float(r.get("odds2")),
        )
        # обязательные поля — ext_id и команды
        if ev.ext_id and (ev.team1 or ev.team2):
            events.append(ev)
    return events

def _safe_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None

async def refresh_line() -> int:
    raw = await fetch_raw()
    parsed = parse_payload(raw)
    return await upsert_events(parsed)
# src/parsing.py
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
    Простая версия:
    — тянем список всех событий
    — фильтруем leagueName == 'KHL'
    — для каждого события тянем маркеты
    — собираем в Event(id, team1, team2, markets)
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
