from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import httpx

API_BASE = (os.getenv("SPORT_API_BASE") or "").strip().rstrip("/")
API_KEY = (os.getenv("SPORT_API_KEY") or "").strip()
TIMEOUT_S = float(os.getenv("SPORT_API_TIMEOUT_S") or "12")


class SportAPIError(RuntimeError):
    pass


@dataclass
class ApiMatch:
    id: str
    sport_slug: str
    title: str
    league: str


class SportAPIClient:
    def __init__(self) -> None:
        if not API_BASE:
            raise SportAPIError("SPORT_API_BASE is not set")
        if not API_KEY:
            raise SportAPIError("SPORT_API_KEY is not set")
        self.base = API_BASE
        self.key = API_KEY
        self.timeout = httpx.Timeout(TIMEOUT_S)

    def _headers(self) -> dict[str, str]:
        # ⚠️ если у провайдера другой заголовок (например X-API-Key) — поменяешь здесь.
        return {
            "Accept": "application/json",
            "Authorization": self.key,
        }

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        url = f"{self.base}{path}"
        async with httpx.AsyncClient(timeout=self.timeout, headers=self._headers()) as client:
            r = await client.get(url, params=params)
            if r.status_code >= 400:
                raise SportAPIError(f"HTTP {r.status_code}: {r.text[:400]}")
            return r.json()

    async def matches_by_date(self, sport_slug: str, day: date) -> list[ApiMatch]:
        sport_slug = (sport_slug or "").strip().lower()
        data = await self._get(f"/v2/{sport_slug}/matches", params={"date": day.isoformat()})

        items = data
        if isinstance(data, dict):
            items = data.get("data") or data.get("results") or data.get("items") or []

        out: list[ApiMatch] = []
        if not isinstance(items, list):
            return out

        for it in items:
            if not isinstance(it, dict):
                continue

            mid = it.get("id")
            if mid is None:
                continue
            match_id = str(mid)

            # максимально мягкий парсер, т.к. структуры могут отличаться
            home = (
                (it.get("homeTeam") or {}).get("name")
                or (it.get("home") or {}).get("name")
                or (it.get("competitors") or [{}])[0].get("name")
                or "Home"
            )
            away = (
                (it.get("awayTeam") or {}).get("name")
                or (it.get("away") or {}).get("name")
                or (it.get("competitors") or [{}, {}])[1].get("name")
                or "Away"
            )
            title = f"{home} — {away}"

            league = (
                (it.get("tournament") or {}).get("name")
                or (it.get("league") or {}).get("name")
                or it.get("league")
                or it.get("competition")
                or ""
            )

            out.append(ApiMatch(id=match_id, sport_slug=sport_slug, title=title, league=str(league or "").strip()))
        return out
