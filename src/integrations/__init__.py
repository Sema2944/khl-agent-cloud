# src/integrations/sport_api.py
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

import httpx


class SportAPIError(Exception):
    pass


@dataclass
class MatchDTO:
    id: str
    sport_slug: str
    title: str
    league: str
    status: str
    start_time: str


@dataclass
class OddsSnapshot:
    raw: Dict[str, Any]
    moneyline: Optional[Dict[str, Any]] = None
    total_main: Optional[Dict[str, Any]] = None
    handicap_main: Optional[Dict[str, Any]] = None


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _auth_headers() -> Dict[str, str]:
    key = _env("SPORT_API_KEY")
    if not key:
        return {}
    hdr = _env("SPORT_API_KEY_HEADER", "Authorization")
    pref = _env("SPORT_API_KEY_PREFIX", "")
    return {hdr: f"{pref}{key}".strip()}


class SportAPIClient:
    """
    Generic Sport Events API client.
    Required ENV:
      - SPORT_API_BASE (e.g. https://api.api-sport.io OR your provider base)
      - SPORT_API_KEY (+ optional header/prefix)
    """

    def __init__(self) -> None:
        self.base = _env("SPORT_API_BASE", "").rstrip("/")
        if not self.base:
            raise SportAPIError("SPORT_API_BASE is missing")
        self.headers = _auth_headers()

    async def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base}{path}"
        timeout = httpx.Timeout(20.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params or {}, headers=self.headers)
        if r.status_code >= 400:
            raise SportAPIError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            raise SportAPIError(f"Bad JSON from API: {r.text[:200]}")

    def _unwrap_list(self, data: Any) -> List[Dict[str, Any]]:
        """
        Провайдеры часто возвращают:
        - {"data":[...]}
        - {"response":[...]}
        - [...]
        """
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for k in ("data", "response", "results", "items"):
                v = data.get(k)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
        return []

    def _unwrap_obj(self, data: Any) -> Dict[str, Any]:
        if isinstance(data, dict):
            for k in ("data", "response", "result", "item"):
                v = data.get(k)
                if isinstance(v, dict):
                    return v
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return {}

    def _match_to_dto(self, raw: Dict[str, Any], sport_slug: str) -> MatchDTO:
        # пытаемся угадать поля разных провайдеров
        mid = str(raw.get("id") or raw.get("eventId") or raw.get("matchId") or "")

        home = (
            (raw.get("homeTeam") or {}).get("translations", {}).get("ru")
            or (raw.get("homeTeam") or {}).get("translation", {}).get("ru")
            or (raw.get("homeTeam") or {}).get("name")
            or raw.get("home")
            or "Home"
        )
        away = (
            (raw.get("awayTeam") or {}).get("translations", {}).get("ru")
            or (raw.get("awayTeam") or {}).get("translation", {}).get("ru")
            or (raw.get("awayTeam") or {}).get("name")
            or raw.get("away")
            or "Away"
        )

        league = (
            (raw.get("tournament") or {}).get("translations", {}).get("ru")
            or (raw.get("tournament") or {}).get("name")
            or (raw.get("league") or {}).get("name")
            or raw.get("leagueName")
            or ""
        )

        status = str(raw.get("status") or raw.get("state") or raw.get("matchStatus") or "")
        start_time = str(raw.get("dateEvent") or raw.get("startTime") or raw.get("start_date") or "")

        title = f"{home} — {away}"
        return MatchDTO(
            id=mid or "unknown",
            sport_slug=sport_slug,
            title=title,
            league=league,
            status=status,
            start_time=start_time,
        )

    async def matches_by_date(self, sport_slug: str, day: date) -> List[MatchDTO]:
        """
        Пробуем типовые пути:
          /v2/<sport>/events/date/<YYYY-MM-DD>
          /v2/<sport>/events?date=YYYY-MM-DD
          /v2/<sport>/matches?date=YYYY-MM-DD
        """
        sport_slug = sport_slug.strip()

        day_s = day.isoformat()
        candidates = [
            (f"/v2/{sport_slug}/events/date/{day_s}", None),
            (f"/v2/{sport_slug}/events", {"date": day_s}),
            (f"/v2/{sport_slug}/matches", {"date": day_s}),
        ]

        last_err = None
        for path, params in candidates:
            try:
                data = await self._get_json(path, params=params)
                items = self._unwrap_list(data)
                if items:
                    return [self._match_to_dto(x, sport_slug) for x in items]
            except Exception as e:
                last_err = e

        raise SportAPIError(f"matches_by_date failed for {sport_slug} {day_s}: {last_err}")

    async def match_details(self, sport_slug: str, match_id: str) -> MatchDTO:
        """
        Типовые пути:
          /v2/<sport>/events/<id>
          /v2/<sport>/matches/<id>
        """
        sport_slug = sport_slug.strip()
        match_id = str(match_id).strip()

        candidates = [
            f"/v2/{sport_slug}/events/{match_id}",
            f"/v2/{sport_slug}/matches/{match_id}",
        ]

        last_err = None
        for path in candidates:
            try:
                data = await self._get_json(path)
                obj = self._unwrap_obj(data)
                if obj:
                    return self._match_to_dto(obj, sport_slug)
            except Exception as e:
                last_err = e

        raise SportAPIError(f"match_details failed: {sport_slug}/{match_id}: {last_err}")

    async def match_odds(self, sport_slug: str, match_id: str) -> OddsSnapshot:
        """
        Типовые пути:
          /v2/<sport>/events/<id>/odds
          /v2/<sport>/matches/<id>/odds
        """
        sport_slug = sport_slug.strip()
        match_id = str(match_id).strip()

        candidates = [
            f"/v2/{sport_slug}/events/{match_id}/odds",
            f"/v2/{sport_slug}/matches/{match_id}/odds",
        ]

        last_err = None
        for path in candidates:
            try:
                data = await self._get_json(path)
                obj = self._unwrap_obj(data)
                if obj:
                    # тут мы не навязываем структуру — просто храним raw
                    return OddsSnapshot(raw=obj)
            except Exception as e:
                last_err = e

        raise SportAPIError(f"match_odds failed: {sport_slug}/{match_id}: {last_err}")
