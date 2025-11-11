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
