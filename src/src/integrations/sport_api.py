# src/integrations/sport_api.py
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

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
    score: str = ""
    country: str = ""
    odds_base: Optional[Dict[str, Any]] = None


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


def _dig(d: Any, *keys: str) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _extract_score(raw: Dict[str, Any]) -> str:
    """
    Пробуем вытащить счёт из разных форматов провайдера.
    Возвращаем строку вида "2:1" или "".
    """
    # 1) готовая строка
    for k in ("score", "finalScore", "result", "scoreStr"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # 2) объект/словарь вида {"home":2,"away":1}
    v = raw.get("score")
    if isinstance(v, dict):
        h = v.get("home") or v.get("homeScore") or v.get("h")
        a = v.get("away") or v.get("awayScore") or v.get("a")
        if h is not None and a is not None:
            return f"{h}:{a}"

    # 3) отдельные поля
    pairs: List[Tuple[Any, Any]] = [
        (raw.get("homeScore"), raw.get("awayScore")),
        (raw.get("homeGoals"), raw.get("awayGoals")),
        (_dig(raw, "scores", "home"), _dig(raw, "scores", "away")),
        (_dig(raw, "result", "home"), _dig(raw, "result", "away")),
        (_dig(raw, "homeTeam", "score"), _dig(raw, "awayTeam", "score")),
    ]
    for h, a in pairs:
        if h is not None and a is not None:
            return f"{h}:{a}"

    # 4) иногда лежит как "goals": {"home":..,"away":..}
    goals = raw.get("goals")
    if isinstance(goals, dict):
        h = goals.get("home")
        a = goals.get("away")
        if h is not None and a is not None:
            return f"{h}:{a}"

    return ""


def _extract_country(raw: Dict[str, Any]) -> str:
    for k in ("country", "leagueCountry", "countryName"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    v = _dig(raw, "tournament", "country")
    if isinstance(v, str) and v.strip():
        return v.strip()

    v = _dig(raw, "league", "country")
    if isinstance(v, str) and v.strip():
        return v.strip()

    v = _dig(raw, "tournament", "category", "name")
    if isinstance(v, str) and v.strip():
        return v.strip()

    return ""


def _extract_odds_base(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    В твоём parsing.py используется odds_base (dict) и из него markets/choices.
    Тут просто пробуем найти похожее поле и вернуть как dict.
    """
    for k in ("odds_base", "oddsBase", "oddsbase", "oddsBaseData"):
        v = raw.get(k)
        if isinstance(v, dict):
            return v

    # иногда odds лежит как {"markets":[...]}
    v = raw.get("odds")
    if isinstance(v, dict) and ("markets" in v or "lines" in v):
        return v

    return None


class SportAPIClient:
    """
    Generic Sport Events API client.
    Required ENV:
      - SPORT_API_BASE (e.g. https://api.api-sport.ru OR your provider base)
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
            _dig(raw, "homeTeam", "translations", "ru")
            or _dig(raw, "homeTeam", "translation", "ru")
            or _dig(raw, "homeTeam", "name")
            or raw.get("home")
            or "Home"
        )
        away = (
            _dig(raw, "awayTeam", "translations", "ru")
            or _dig(raw, "awayTeam", "translation", "ru")
            or _dig(raw, "awayTeam", "name")
            or raw.get("away")
            or "Away"
        )

        league = (
            _dig(raw, "tournament", "translations", "ru")
            or _dig(raw, "tournament", "name")
            or _dig(raw, "league", "name")
            or raw.get("leagueName")
            or ""
        )

        status = str(raw.get("status") or raw.get("state") or raw.get("matchStatus") or "")
        start_time = str(raw.get("dateEvent") or raw.get("startTime") or raw.get("start_date") or "")

        score = _extract_score(raw)
        country = _extract_country(raw)
        odds_base = _extract_odds_base(raw)

        title = f"{home} — {away}"
        return MatchDTO(
            id=mid or "unknown",
            sport_slug=sport_slug,
            title=title,
            league=league,
            status=status,
            start_time=start_time,
            score=score,
            country=country,
            odds_base=odds_base,
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
        Поскольку у твоего провайдера (api.api-sport.ru) матч по id иногда НЕ лежит по /v2/<sport>/<id>,
        пробуем несколько вариантов:
          /v2/<sport>/events/<id>
          /v2/<sport>/matches/<id>
          /v2/<sport>/match/<id>
          /v2/<sport>/event/<id>
          /v2/<sport>/matches?id=<id>
          /v2/<sport>/events?id=<id>
          /v2/<sport>/<id>                  (оставляем последним, на случай другого провайдера)
        """
        sport_slug = sport_slug.strip()
        match_id = str(match_id).strip()

        candidates: List[Tuple[str, Optional[Dict[str, Any]]]] = [
            (f"/v2/{sport_slug}/events/{match_id}", None),
            (f"/v2/{sport_slug}/matches/{match_id}", None),
            (f"/v2/{sport_slug}/match/{match_id}", None),
            (f"/v2/{sport_slug}/event/{match_id}", None),
            (f"/v2/{sport_slug}/matches", {"id": match_id}),
            (f"/v2/{sport_slug}/events", {"id": match_id}),
            (f"/v2/{sport_slug}/{match_id}", None),
        ]

        last_err = None
        for path, params in candidates:
            try:
                data = await self._get_json(path, params=params)
                # если это был список — возьмём 1й
                items = self._unwrap_list(data)
                if items:
                    return self._match_to_dto(items[0], sport_slug)

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
        + несколько дополнительных fallback
        """
        sport_slug = sport_slug.strip()
        match_id = str(match_id).strip()

        candidates: List[Tuple[str, Optional[Dict[str, Any]]]] = [
            (f"/v2/{sport_slug}/events/{match_id}/odds", None),
            (f"/v2/{sport_slug}/matches/{match_id}/odds", None),
            (f"/v2/{sport_slug}/odds/{match_id}", None),
            (f"/v2/{sport_slug}/odds", {"id": match_id}),
        ]

        last_err = None
        for path, params in candidates:
            try:
                data = await self._get_json(path, params=params)
                obj = self._unwrap_obj(data)
                if obj:
                    return OddsSnapshot(raw=obj)
                items = self._unwrap_list(data)
                if items:
                    return OddsSnapshot(raw=items[0])
            except Exception as e:
                last_err = e

        raise SportAPIError(f"match_odds failed: {sport_slug}/{match_id}: {last_err}")
